"""One collection for one collection month across all source accounts."""

import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import partial
from statistics import median
from typing import Any, Protocol

from invoice_collector import trail
from invoice_collector.archive import Archive, pdf_sha256
from invoice_collector.charges import summarise
from invoice_collector.checks import History, history_checks, reading_checks, summary_of
from invoice_collector.classifier import (
    ClassificationFailed,
    Classifier,
    text_of,
    vendor_from_sender,
)
from invoice_collector.domain import (
    Attachment,
    BillingSignal,
    CollectionMonth,
    Doubt,
    Email,
    EmailState,
    ExpectedVendor,
    Extraction,
    Gap,
    InvoiceFormat,
    StartedBy,
    SummaryRow,
    UpcomingCharge,
)
from invoice_collector.exchange_rates import ExchangeRates, ExchangeRateUnavailable
from invoice_collector.extractor import (
    ExtractionFailed,
    Extractor,
    NotABillingDocument,
    content_hash,
    pdf_problem,
    pdf_text,
)
from invoice_collector.ledger import CollectedDocument, Ledger, PendingDocument
from invoice_collector.mail_source import MailSource, SourceAccountUnavailable
from invoice_collector.naming import filename
from invoice_collector.portal import LoginGated, PortalFetcher, PortalFetchFailed
from invoice_collector.reconciler import reconcile_month, suggested_vendors, vendor_key
from invoice_collector.renderer import Renderer, RenderFailed
from invoice_collector.routing import Attachments, Body, NotBilling, PortalLink, route
from invoice_collector.summary import SummaryWriter
from invoice_collector.vendor_matcher import (
    RulesFirstVendorMatcher,
    VendorMatcher,
    VendorMatchFailed,
)


@dataclass(frozen=True)
class RunResult:
    summary: list[SummaryRow]
    warnings: list[str] = field(default_factory=list[str])
    gaps: list[Gap] = field(default_factory=list[Gap])
    upcoming: list[UpcomingCharge] = field(default_factory=list[UpcomingCharge])
    # Source accounts that could not be read, each with the reason.
    failed_source_accounts: dict[str, str] = field(default_factory=dict[str, str])
    suggested_vendors: list[ExpectedVendor] = field(default_factory=list[ExpectedVendor])
    # Billing documents held for a person to confirm.
    pending: list[PendingDocument] = field(default_factory=list[PendingDocument])
    # The run as the ledger records it.
    run_id: int | None = None


@dataclass(frozen=True)
class Pipeline:
    """The modules a run is composed from."""

    classifier: Classifier
    extractor: Extractor
    renderer: Renderer
    portal_fetcher: PortalFetcher
    exchange_rates: ExchangeRates
    archive: Archive
    ledger: Ledger
    # Reads a document again when the first reading is doubted. See ADR 0008.
    stronger_extractor: Extractor | None = None
    # Matches the vendor a document names to the expected vendor list. See ADR 0009.
    vendor_matcher: VendorMatcher = field(default_factory=RulesFirstVendorMatcher)
    # When each step in the history of a billing document happened. See trail.py.
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)


@dataclass(frozen=True)
class Settings:
    # An invoice is often emailed a day or two before or after the date printed on it.
    search_window_days: int = 7
    # How far from a vendor's usual amount a total may be before it is doubted.
    anomaly_threshold: Decimal = Decimal("0.30")
    # How long to wait before each further attempt at an email whose examination raised
    # something unanticipated, such as a dropped connection. One wait per retry.
    retry_delays: tuple[float, ...] = (10.0, 20.0)


# The reason recorded when a person, on the Review screen, judges an email not to be a
# billing document.
JUDGED_NOT_BILLING = "a person judged it not to be a billing document"


@dataclass(frozen=True)
class _Found:
    """A billing document found in an email, before it is fetched or read."""

    identity: str
    produce: Callable[[], bytes | LoginGated]
    # How it was found, for its history.
    details: Mapping[str, Any]


class _ManualDownloadNeeded(Exception):
    pass


@dataclass(frozen=True)
class _Reading:
    """A billing document fetched and read, before it is weighed against the ledger."""

    pdf: bytes
    extraction: Extraction
    # Doubts about the reading itself, found without looking at the ledger.
    doubts: tuple[Doubt, ...]
    read_again: bool
    # None when the document belongs to another month, or no rate is known.
    rate: Decimal | None
    # The vendor as the document named it, when the extraction carries the expected
    # vendor's spelling instead.
    vendor_as_read: str | None = None
    # The PDF could not be opened, so the extraction holds no reading, only a start.
    unopened: bool = False


def _as_charged(extraction: Extraction) -> Extraction:
    """A credit note records money returned, so its amount is negative."""
    if extraction.document_type == "credit_note":
        return replace(extraction, total=-abs(extraction.total))
    return extraction


class Examination(Protocol):
    """Examining one email, ready to be performed."""

    @property
    def email(self) -> Email: ...

    def __call__(self) -> None:
        """Examines the email and records its outcome.

        An outcome the pipeline anticipates, failures included, is recorded. Anything
        else is raised, and nothing is recorded for the email.
        """
        ...

    def fail(self, reason: str) -> None:
        """Records the email as failed, for an examination that raised."""
        ...


# Performs every examination it is given, each exactly once.
ExamineAll = Callable[[Sequence[Examination]], None]


# Failures of the code itself. Performing it again gives the same failure.
NOT_WORTH_RETRYING: tuple[type[Exception], ...] = (
    TypeError,
    AttributeError,
    NameError,
    AssertionError,
    NotImplementedError,
    LookupError,
)


def worth_retrying(error: BaseException) -> bool:
    """Whether an examination that raised this may succeed if performed again."""
    return isinstance(error, Exception) and not isinstance(error, NOT_WORTH_RETRYING)


def failure_reason(error: object) -> str:
    """The reason recorded for an email whose examination raised."""
    return f"could not be examined: {type(error).__name__}: {error}"


def one_after_another(
    examinations: Sequence[Examination],
    *,
    retry_delays: Sequence[float] = Settings.retry_delays,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Performs each examination in turn.

    One that raises is performed again after each delay, unless the fault is in the code.
    Nothing is recorded for an email until its examination completes, so this is safe.
    When the retries are spent, the email is recorded as failed and the next goes on.
    """
    for examination in examinations:
        for delay in (*retry_delays, None):
            try:
                examination()
                break
            except Exception as error:
                if delay is None or not worth_retrying(error):
                    examination.fail(failure_reason(error))
                    break
                sleep(delay)


class _Examination:
    """Examines one email and records its outcome."""

    def __init__(
        self,
        month: CollectionMonth,
        email: Email,
        pipeline: Pipeline,
        warnings: list[str],
        settings: Settings,
        deciding: threading.Lock,
    ) -> None:
        self._month = month
        self._email = email
        self._pipeline = pipeline
        # Shared by every examination of the run. Only appended to, which is safe
        # from several threads at once.
        self._warnings = warnings
        self._settings = settings
        # Shared by every examination of the run, so each weighs its documents against
        # the ledger and records the outcome with nothing else recorded in between.
        self._deciding = deciding
        self._collected_before = False
        self._invoice_format: InvoiceFormat | None = None
        self._portal_link: str | None = None
        # The expected vendor each name found in the email stands for, once worked out,
        # with what matched it.
        self._spellings: dict[str, tuple[str | None, str | None]] = {}
        # What happened to the email and its documents, recorded with its outcome.
        self._events: list[trail.Event] = []
        # A step for each attempt that raised and was performed again. Kept when the
        # steps of an attempt that raised are dropped, since nothing of it was recorded.
        self._retries: list[trail.Event] = []
        self._failed_attempt: str | None = None

    @property
    def email(self) -> Email:
        return self._email

    def __call__(self) -> None:
        if self._failed_attempt is not None:
            self._events = list(self._retries)
            details = {"attempt": len(self._retries) + 2, "after": self._failed_attempt}
            self._event(trail.RETRIED, trail.RUN, details)
            self._retries = list(self._events)
            self._failed_attempt = None
        try:
            self._attempt()
        except Exception as error:
            # Nothing was recorded for the email, so neither are the steps of this attempt.
            self._events = list(self._retries)
            self._failed_attempt = failure_reason(error)
            raise

    def _attempt(self) -> None:
        if not self._still_open():
            return
        recorded = self._pipeline.ledger.state_of(self._email)
        if recorded in {
            (self._month, EmailState.COLLECTED),
            (self._month, EmailState.NEEDS_REVIEW),
        }:
            # Classified by an earlier run, which found billing documents in it. Asking the
            # classifier again costs a call and can only take away, so the documents are
            # looked for again straight away; those in the ledger are not fetched or read.
            self._collect()
            return
        self._examine()

    def fail(self, reason: str) -> None:
        # By the same rules as any other outcome: it never replaces a collection.
        self._events = list(self._retries)
        if self._still_open():
            self._record(EmailState.FAILED, reason)

    def _still_open(self) -> bool:
        collected_in = self._pipeline.ledger.collected_in(self._email)
        if collected_in is not None and collected_in != self._month:
            return False  # Already collected for the month its invoice date falls in.
        self._collected_before = collected_in is not None
        return True

    def _examine(self) -> None:

        try:
            classification = self._pipeline.classifier.classify(self._email)
        except ClassificationFailed as failure:
            self._record(EmailState.FAILED, str(failure))
            return
        self._event(trail.CLASSIFIED, classification.by, trail.classified(classification))

        match classification.kind:
            case "not_billing":
                self._record(EmailState.SKIPPED, "not a billing email")
            case "payment_failed" | "renewal_reminder" as kind:
                vendor = classification.vendor
                if vendor:
                    vendor = self._matched(vendor, text_of(self._email), None) or vendor
                signal = BillingSignal(
                    kind=kind,
                    vendor=vendor,
                    source_account=self._email.source_account,
                    message_id=self._email.message_id,
                    subject=self._email.subject,
                    received_at=self._email.received_at,
                )
                reason = f"billing signal: {kind.replace('_', ' ')}"
                self._record(EmailState.SKIPPED, reason, signal=signal)
            case _:
                self._collect()

    def _record(
        self,
        state: EmailState,
        reason: str | None = None,
        *,
        documents: tuple[CollectedDocument, ...] = (),
        pending: tuple[PendingDocument, ...] = (),
        signal: BillingSignal | None = None,
    ) -> None:
        # An email with nothing collected from it belongs to the month it arrived in.
        # One that arrived outside the month is left for that month's run.
        if not (documents or pending) and not self._month.contains(self._email.received_at):
            return
        # A run never takes away what an earlier run collected. A model that is down,
        # or that answers differently today, must not make a billing document vanish.
        if not documents and self._collected_before:
            self._warnings.append(
                f"{self._email.subject}: kept what was collected before (this run: {reason})"
            )
            return
        self._pipeline.ledger.record(
            self._month,
            self._email,
            state,
            reason=reason,
            invoice_format=self._invoice_format,
            portal_link=self._portal_link,
            documents=documents,
            pending=pending,
            signal=signal,
        )
        self._pipeline.ledger.record_events(self._events)
        self._events.clear()

    def _event(
        self,
        kind: str,
        actor: str | None,
        details: Mapping[str, Any],
        content_hash: str | None = None,
    ) -> None:
        self._events.append(
            trail.Event(
                kind=kind,
                source_account=self._email.source_account,
                message_id=self._email.message_id,
                happened_at=self._pipeline.clock(),
                content_hash=content_hash,
                actor=actor,
                details=details,
            )
        )

    def _found(self) -> list[_Found] | None:
        found = self._found_in_email()
        for each in found or []:
            self._event(trail.FOUND, trail.RUN, each.details, each.identity)
        return found

    def _found_in_email(self) -> list[_Found] | None:
        renderer, fetcher = self._pipeline.renderer, self._pipeline.portal_fetcher
        routed = route(self._email)
        match routed:
            case NotBilling(reason):
                self._record(EmailState.SKIPPED, reason)
                return None
            case Attachments(attachments):
                self._invoice_format = routed.invoice_format
                # Keyed by content, so the same PDF attached twice is one document.
                pdfs: dict[str, Attachment] = {}
                for attachment in attachments:
                    pdfs.setdefault(content_hash(attachment.content), attachment)
                return [
                    _Found(
                        digest,
                        lambda pdf=a.content: pdf,
                        trail.found(routed.invoice_format, attachment=a.filename),
                    )
                    for digest, a in pdfs.items()
                ]
            case Body(html):
                self._invoice_format = routed.invoice_format
                identity = content_hash(" ".join(html.split()).encode())
                return [
                    _Found(
                        identity,
                        lambda: renderer.render_html(html),
                        trail.found(routed.invoice_format),
                    )
                ]
            case PortalLink(url):
                self._invoice_format = routed.invoice_format
                self._portal_link = url
                return [
                    _Found(
                        content_hash(url.encode()),
                        lambda: fetcher.fetch(url),
                        trail.found(routed.invoice_format, url=url),
                    )
                ]

    def _rate(self, extraction: Extraction) -> Decimal | None:
        try:
            return self._pipeline.exchange_rates.to_rupees(
                extraction.currency, extraction.invoice_date
            )
        except ExchangeRateUnavailable as unavailable:
            self._warnings.append(f"{extraction.vendor} {extraction.invoice_date}: {unavailable}")
            return None

    def _expected_spelling(self, named: str, text: str) -> tuple[str | None, str | None]:
        """The expected vendor a name stands for, as the expected vendor list spells it,
        and what matched it: "rules", or the model's name.

        A name that differs from a listed one only in case, punctuation or a legal suffix
        is that vendor. Otherwise the matcher is asked, with the text the name came from.
        The vendor is None when the name stands for no expected vendor, or the matcher
        failed.
        """
        if named in self._spellings:
            return self._spellings[named]
        expected = [
            v.vendor for v in self._pipeline.ledger.expected_vendors() if v.status == "expected"
        ]
        spelling = next((v for v in expected if vendor_key(v) == vendor_key(named)), None)
        by = trail.RULES if spelling is not None else None
        if spelling is None:
            try:
                match = self._pipeline.vendor_matcher.match(f"{named}\n{text}", expected)
            except VendorMatchFailed as failure:
                self._warnings.append(
                    f"{self._email.subject}: {named} could not be matched to an expected "
                    f"vendor ({failure}), so it is kept as read"
                )
            else:
                spelling, by = match.vendor, match.by
        self._spellings[named] = (spelling, by)
        return spelling, by

    def _matched(self, named: str, text: str, identity: str | None) -> str | None:
        """The expected vendor's spelling of a name, None when it stands for no expected
        vendor. A match that changes the name is a step in the history."""
        spelling, by = self._expected_spelling(named, text)
        if spelling is not None and spelling != named:
            self._event(trail.MATCHED, by, trail.matched(named, spelling), identity)
        return spelling

    def _as_expected(
        self, extraction: Extraction, text: str, identity: str
    ) -> tuple[Extraction, str | None]:
        """The extraction under the expected vendor's spelling, and the name it replaced."""
        spelling = self._matched(extraction.vendor, text, identity)
        if spelling is None or spelling == extraction.vendor:
            return extraction, None
        return replace(extraction, vendor=spelling), extraction.vendor

    def _history(self, extraction: Extraction, identity: str) -> History:
        ledger, vendor = self._pipeline.ledger, vendor_key(extraction.vendor)
        listed = [
            v
            for v in ledger.expected_vendors()
            if v.status == "expected" and vendor_key(v.vendor) == vendor and v.usual_amount
        ]
        earlier = [
            d.extraction
            for month in ledger.months()
            if (month.year, month.month) < (self._month.year, self._month.month)
            for d in ledger.documents(month)
            if vendor_key(d.extraction.vendor) == vendor
            and d.extraction.document_type != "credit_note"
        ]
        this_month = {
            d.content_hash
            for d in ledger.documents(self._month)
            if vendor_key(d.extraction.vendor) == vendor
            and d.extraction.document_type == extraction.document_type
            and d.content_hash != identity
        }
        if listed:
            usual, currency = listed[0].usual_amount, listed[0].currency
        elif earlier:
            latest = max(earlier, key=lambda e: e.invoice_date)
            same = [e.total for e in earlier if e.currency == latest.currency]
            usual = Decimal(median(same)).quantize(Decimal("0.01"))
            currency = latest.currency
        else:
            usual, currency = None, None
        return History(usual, currency, already_this_month=len(this_month))

    def _read(self, pdf: bytes, identity: str) -> _Reading:
        """What the document says, the doubts about the reading, and whether it was read again.

        Doubts that depend on what else the ledger holds are found later, by _document.
        """
        try:
            extraction = _as_charged(self._pipeline.extractor.extract(pdf))
        except ExtractionFailed:
            problem = pdf_problem(pdf)
            if problem is None:
                raise  # The PDF opens: a later run may read it, so the email fails.
            return self._unopened(pdf, problem, identity)
        self._event(trail.READ, extraction.by, trail.read(extraction), identity)
        reading = self._reading_doubts(extraction, identity)
        read_again = False
        stronger = self._pipeline.stronger_extractor
        # Only a doubt about the reading is worth a second reading. A total far from
        # the usual one was read correctly as far as anyone knows.
        if reading and stronger is not None:
            try:
                second = _as_charged(stronger.extract(pdf))
            except (ExtractionFailed, NotABillingDocument) as failure:
                # The first reading stands, with its doubts.
                self._event(trail.READ_AGAIN_FAILED, None, {"reason": str(failure)}, identity)
            else:
                read_again = True
                details = trail.read_again(extraction, second)
                self._event(trail.READ_AGAIN, second.by, details, identity)
                extraction = second
                reading = self._reading_doubts(extraction, identity)
        if not self._month.contains(extraction.invoice_date):
            return _Reading(pdf, extraction, tuple(reading), read_again, None)
        # Matched from the document's own text, as the eval scored it, or the email's.
        text = pdf_text(pdf)[0] or text_of(self._email)
        extraction, as_read = self._as_expected(extraction, text, identity)
        rate = self._rate(extraction)
        return _Reading(pdf, extraction, tuple(reading), read_again, rate, as_read)

    def _unopened(self, pdf: bytes, problem: str, identity: str) -> _Reading:
        """A PDF that cannot be opened, damaged or password-protected, held as it is.

        Nothing was read, so nothing is guessed: the fields hold what the email gives, the
        sender and the day it arrived, and no amount, for a person to fill in from the
        document. See ADR 0005.
        """
        email = self._email
        named = vendor_from_sender(email) or email.sender
        start = Extraction(
            document_type="invoice",
            vendor=named,
            invoice_date=email.received_at.date(),
            total=Decimal("0.00"),
            # ISO 4217's code for no currency.
            currency="XXX",
            confidence="low",
        )
        self._event(trail.UNOPENED, trail.RUN, {"problem": problem}, identity)
        extraction, as_read = self._as_expected(start, text_of(email), identity)
        why = f"{problem}, so nothing could be read from it"
        doubts = (
            Doubt(None, why),
            Doubt("vendor", f"taken from the sender of the email: {why}"),
            Doubt("invoice_date", f"the day the email arrived: {why}"),
            Doubt("total", f"not read: {why}"),
            Doubt("currency", f"not read: {why}"),
        )
        return _Reading(pdf, extraction, doubts, False, None, as_read, unopened=True)

    def _history_doubts(
        self, extraction: Extraction, identity: str, threshold: Decimal
    ) -> list[Doubt]:
        history = self._history(extraction, identity)
        results = history_checks(extraction, history, threshold)
        self._event(trail.CHECKED, trail.RUN, trail.checked("history", results), identity)
        return [doubt for result in results for doubt in result.doubts]

    def _reading_doubts(self, extraction: Extraction, identity: str) -> list[Doubt]:
        results = reading_checks(extraction, self._email)
        self._event(trail.CHECKED, trail.RUN, trail.checked("reading", results), identity)
        return [doubt for result in results for doubt in result.doubts]

    def _in_ledger(self, found: _Found) -> bool:
        """Whether the document is already held or collected, and so is not read again."""
        ledger = self._pipeline.ledger
        return (
            ledger.pending_with(found.identity) is not None
            or ledger.document_with(found.identity) is not None
        )

    def _fetch_and_read(self, found: _Found) -> _Reading:
        """The document fetched and read.

        Raises PortalFetchFailed, RenderFailed, _ManualDownloadNeeded, NotABillingDocument
        or ExtractionFailed.
        """
        produced = found.produce()
        if isinstance(produced, LoginGated):
            raise _ManualDownloadNeeded
        return self._read(produced, found.identity)

    def _document(
        self, found: _Found, reading: _Reading | None
    ) -> CollectedDocument | PendingDocument | CollectionMonth:
        """The billing document weighed against the ledger, or the other collection month
        it belongs to.

        The reading is None for a document that was in the ledger when it was looked for.
        Raises as _fetch_and_read does.
        """
        ledger = self._pipeline.ledger
        held = ledger.pending_with(found.identity)
        if held is not None:
            # Waiting for a person. It is not read again until they have decided.
            return held

        known = ledger.document_with(found.identity)
        if known is not None:
            if not self._month.contains(known.extraction.invoice_date):
                return CollectionMonth.of(known.extraction.invoice_date)
            extraction, as_read = known.extraction, known.vendor_as_read
            if as_read is None:
                # Collected before under the name as read, perhaps before vendors were
                # matched. It is not read again, so the name is matched with the email's
                # text. Its PDF keeps its file and name. A name that was matched, or that
                # a person corrected, already has what the document said beside it.
                extraction, as_read = self._as_expected(
                    extraction, text_of(self._email), found.identity
                )
            rate = known.inr_rate if known.inr_rate is not None else self._rate(extraction)
            return CollectedDocument(found.identity, extraction, known.file_link, rate, as_read)

        if reading is None:
            # It was held when it was looked for, and a person has rejected it since.
            reading = self._fetch_and_read(found)
        extraction = reading.extraction
        if not self._month.contains(extraction.invoice_date):
            other = CollectionMonth.of(extraction.invoice_date)
            self._event(trail.LEFT_FOR_MONTH, trail.RUN, {"month": str(other)}, found.identity)
            return other
        threshold = self._settings.anomaly_threshold
        history = (
            [] if reading.unopened else self._history_doubts(extraction, found.identity, threshold)
        )
        doubts = [*reading.doubts, *history]
        folder = f"{self._month}/pending" if doubts else str(self._month)
        link = self._pipeline.archive.save(folder, filename(extraction), reading.pdf)
        self._filed(found.identity, extraction, link, reading.rate, converted=not reading.unopened)
        if doubts:
            return PendingDocument(
                found.identity,
                extraction,
                link,
                tuple(doubts),
                reading.rate,
                reading.read_again,
                vendor_as_read=reading.vendor_as_read,
                pdf_sha256=pdf_sha256(reading.pdf),
            )
        return CollectedDocument(
            found.identity,
            extraction,
            link,
            reading.rate,
            reading.vendor_as_read,
            pdf_sha256=pdf_sha256(reading.pdf),
        )

    def _filed(
        self,
        identity: str,
        extraction: Extraction,
        link: str,
        rate: Decimal | None,
        *,
        converted: bool = True,
    ) -> None:
        """Records where the PDF was filed and, unless nothing was read, its rate to rupees."""
        self._event(trail.FILED, trail.RUN, trail.filed(link), identity)
        if converted:
            self._event(trail.CONVERTED, trail.RUN, trail.converted(extraction, rate), identity)

    def _collect(self) -> None:
        found = self._found()
        if found is None:
            return

        try:
            # Fetching and reading take most of the time, so examinations may do them at
            # once. A document already in the ledger is not fetched or read again.
            readings = [
                None if self._in_ledger(each) else self._fetch_and_read(each) for each in found
            ]
            # A document collected before is matched again here, outside the lock below,
            # since the matcher may ask a model.
            for each, reading in zip(found, readings, strict=True):
                known = None if reading else self._pipeline.ledger.document_with(each.identity)
                if known is not None and known.vendor_as_read is None:
                    self._expected_spelling(known.extraction.vendor, text_of(self._email))
            # Weighing each document against the ledger and recording the outcome are
            # done one email at a time. Two invoices from one vendor weighed at once would
            # each miss the other, and neither would be doubted as the second this month.
            with self._deciding:
                self._decide(found, readings)
        except _ManualDownloadNeeded:
            self._record(EmailState.NEEDS_REVIEW, "manual download needed")
        except NotABillingDocument as finding:
            self._record(EmailState.SKIPPED, str(finding))
        except (PortalFetchFailed, RenderFailed, ExtractionFailed) as failure:
            self._record(EmailState.FAILED, str(failure))

    def _decide(self, found: Sequence[_Found], readings: Sequence[_Reading | None]) -> None:
        documents: list[CollectedDocument] = []
        pending: list[PendingDocument] = []
        other_months: list[CollectionMonth] = []
        for each, reading in zip(found, readings, strict=True):
            document = self._document(each, reading)
            if isinstance(document, CollectionMonth):
                other_months.append(document)
            elif isinstance(document, PendingDocument):
                pending.append(document)
            else:
                documents.append(document)

        for document in pending:
            details = trail.held(document.doubts)
            self._event(trail.HELD, trail.RUN, details, document.content_hash)
        if pending:
            for document in documents:
                details = trail.held((), waits_with_email=True)
                self._event(trail.HELD, trail.RUN, details, document.content_hash)
        else:
            for document in documents:
                self._event(trail.COLLECTED, trail.RUN, {}, document.content_hash)

        if pending:
            # The whole email waits, so the documents that raised no doubt wait with it.
            waiting = (
                *pending,
                *(
                    PendingDocument(
                        d.content_hash,
                        d.extraction,
                        d.file_link,
                        (),
                        d.inr_rate,
                        vendor_as_read=d.vendor_as_read,
                        pdf_sha256=d.pdf_sha256,
                    )
                    for d in documents
                ),
            )
            doubts = [doubt for p in pending for doubt in p.doubts]
            self._record(EmailState.NEEDS_REVIEW, summary_of(doubts), pending=waiting)
        elif documents:
            self._record(EmailState.COLLECTED, documents=tuple(documents))
        else:
            self._record(EmailState.SKIPPED, f"belongs to collection month {other_months[0]}")


def collect(
    month: CollectionMonth,
    *,
    sources: Sequence[MailSource],
    pipeline: Pipeline,
    summary_writers: Sequence[SummaryWriter],
    settings: Settings | None = None,
    examine_all: ExamineAll | None = None,
    started_by: StartedBy = "command_line",
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> RunResult:
    """Collects the month. examine_all decides how the emails found are examined; by
    default one after another, each retried as settings say.

    The run is recorded in the ledger as it starts, and again as it finishes with the
    outcome of each email it examined. A run that crashes is left recorded as unfinished.
    """
    settings = settings or Settings()
    if examine_all is None:
        examine_all = partial(one_after_another, retry_delays=settings.retry_delays)
    window = timedelta(days=settings.search_window_days)
    warnings: list[str] = []
    ledger = pipeline.ledger
    failed_source_accounts: dict[str, str] = {}
    # An email a person judged not to be a billing document is left as they decided.
    # Examined again, it would be read again, doubted again and held for review again.
    judged = {
        (e.source_account, e.message_id)
        for each in ledger.months()
        for e in ledger.examined_emails(each)
        if e.state is EmailState.SKIPPED and e.reason == JUDGED_NOT_BILLING
    }
    deciding = threading.Lock()
    examinations: list[Examination] = []
    run_id = ledger.start_run(month, started_by, now())
    for source in sources:
        try:
            emails = source.emails_between(month.start - window, month.end + window)
        except SourceAccountUnavailable as unavailable:
            # The other source accounts are still read.
            failed_source_accounts[source.source_account] = str(unavailable)
            ledger.record_sync(month, source.source_account, str(unavailable), run_id)
            continue
        ledger.record_sync(month, source.source_account, run_id=run_id)
        examinations.extend(
            _Examination(month, email, pipeline, warnings, settings, deciding)
            for email in emails
            if (email.source_account, email.message_id) not in judged
        )
    examine_all(examinations)

    summary = summarise(ledger.documents(month))
    suggestions = _suggest_vendors(ledger)
    reconciliation = reconcile_month(ledger, month, summary)
    for writer in summary_writers:
        writer.write(summary)
    examined = {(e.email.source_account, e.email.message_id) for e in examinations}
    states = Counter(
        e.state
        for e in ledger.examined_emails(month)
        if (e.source_account, e.message_id) in examined
    )
    # The cost of model calls is not metered in a run yet, so it is left unknown.
    ledger.finish_run(run_id, now(), states)
    return RunResult(
        summary=summary,
        warnings=warnings,
        gaps=reconciliation.gaps,
        upcoming=reconciliation.upcoming,
        failed_source_accounts=failed_source_accounts,
        suggested_vendors=suggestions,
        pending=ledger.pending(month),
        run_id=run_id,
    )


def _suggest_vendors(ledger: Ledger) -> list[ExpectedVendor]:
    """Adds vendors that have billed the company and are on no list, as suggestions.

    A suggestion with no charge left behind it is withdrawn: its billing documents have
    since been matched to an expected vendor. See ADR 0016.
    """
    charges = [row for month in ledger.months() for row in summarise(ledger.documents(month))]
    charged = {vendor_key(row.vendor) for row in charges if row.document_type != "credit_note"}
    for vendor in ledger.expected_vendors():
        if vendor.status == "suggested" and vendor_key(vendor.vendor) not in charged:
            ledger.remove_expected_vendor(vendor.vendor)
    suggestions = suggested_vendors(ledger.expected_vendors(), charges)
    for vendor in suggestions:
        ledger.save_expected_vendor(vendor)
    return [v for v in ledger.expected_vendors() if v.status == "suggested"]


def seed_expected_vendors(
    ledger: Ledger, vendors: Sequence[ExpectedVendor], addresses: Mapping[str, str] | None = None
) -> None:
    """Fills the expected vendor list on first run. A list that has entries is left alone.

    addresses maps a sample source account to the real one that receives its emails. It
    applies to the vendors given, and to vendors already on the list, so a list filled from
    the sample file by an earlier run names the real source accounts too.
    """
    moved = dict(addresses or {})

    def readdressed(vendor: ExpectedVendor) -> ExpectedVendor:
        account = vendor.source_account
        if account is None or account not in moved:
            return vendor
        return replace(vendor, source_account=moved[account])

    listed = ledger.expected_vendors()
    for vendor in listed or vendors:
        if not listed or readdressed(vendor) != vendor:
            ledger.save_expected_vendor(readdressed(vendor))
