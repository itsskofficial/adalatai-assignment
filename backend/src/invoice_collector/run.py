"""One collection for one collection month across all source accounts."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import timedelta
from decimal import Decimal
from statistics import median

from invoice_collector.archive import Archive
from invoice_collector.charges import summarise
from invoice_collector.checks import History, history_doubts, reading_doubts, summary_of
from invoice_collector.classifier import ClassificationFailed, Classifier
from invoice_collector.domain import (
    BillingSignal,
    CollectionMonth,
    Doubt,
    Email,
    EmailState,
    ExpectedVendor,
    Extraction,
    Gap,
    InvoiceFormat,
    SummaryRow,
    UpcomingCharge,
)
from invoice_collector.exchange_rates import ExchangeRates, ExchangeRateUnavailable
from invoice_collector.extractor import (
    ExtractionFailed,
    Extractor,
    NotABillingDocument,
    content_hash,
)
from invoice_collector.ledger import CollectedDocument, Ledger, PendingDocument
from invoice_collector.mail_source import MailSource, SourceAccountUnavailable
from invoice_collector.naming import filename
from invoice_collector.portal import LoginGated, PortalFetcher, PortalFetchFailed
from invoice_collector.reconciler import reconcile_month, suggested_vendors, vendor_key
from invoice_collector.renderer import Renderer, RenderFailed
from invoice_collector.routing import Attachments, Body, NotBilling, PortalLink, route
from invoice_collector.summary import SummaryWriter


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


@dataclass(frozen=True)
class Settings:
    # An invoice is often emailed a day or two before or after the date printed on it.
    search_window_days: int = 7
    # How far from a vendor's usual amount a total may be before it is doubted.
    anomaly_threshold: Decimal = Decimal("0.30")


@dataclass(frozen=True)
class _Found:
    """A billing document found in an email, before it is fetched or read."""

    identity: str
    produce: Callable[[], bytes | LoginGated]


class _ManualDownloadNeeded(Exception):
    pass


def _as_charged(extraction: Extraction) -> Extraction:
    """A credit note records money returned, so its amount is negative."""
    if extraction.document_type == "credit_note":
        return replace(extraction, total=-abs(extraction.total))
    return extraction


class _Examination:
    """Examines one email and records its outcome."""

    def __init__(
        self,
        month: CollectionMonth,
        email: Email,
        pipeline: Pipeline,
        warnings: list[str],
        settings: Settings,
    ) -> None:
        self._month = month
        self._email = email
        self._pipeline = pipeline
        self._warnings = warnings
        self._settings = settings
        self._collected_before = False
        self._invoice_format: InvoiceFormat | None = None
        self._portal_link: str | None = None

    def run(self) -> None:
        collected_in = self._pipeline.ledger.collected_in(self._email)
        if collected_in is not None and collected_in != self._month:
            return  # Already collected for the month its invoice date falls in.
        self._collected_before = collected_in is not None

        try:
            classification = self._pipeline.classifier.classify(self._email)
        except ClassificationFailed as failure:
            self._record(EmailState.FAILED, str(failure))
            return

        match classification.kind:
            case "not_billing":
                self._record(EmailState.SKIPPED, "not a billing email")
            case "payment_failed" | "renewal_reminder" as kind:
                signal = BillingSignal(
                    kind=kind,
                    vendor=classification.vendor,
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

    def _found(self) -> list[_Found] | None:
        renderer, fetcher = self._pipeline.renderer, self._pipeline.portal_fetcher
        routed = route(self._email)
        match routed:
            case NotBilling(reason):
                self._record(EmailState.SKIPPED, reason)
                return None
            case Attachments(attachments):
                self._invoice_format = routed.invoice_format
                # Keyed by content, so the same PDF attached twice is one document.
                pdfs = {content_hash(a.content): a.content for a in attachments}
                return [_Found(digest, lambda pdf=pdf: pdf) for digest, pdf in pdfs.items()]
            case Body(html):
                self._invoice_format = routed.invoice_format
                identity = content_hash(" ".join(html.split()).encode())
                return [_Found(identity, lambda: renderer.render_html(html))]
            case PortalLink(url):
                self._invoice_format = routed.invoice_format
                self._portal_link = url
                return [_Found(content_hash(url.encode()), lambda: fetcher.fetch(url))]

    def _rate(self, extraction: Extraction) -> Decimal | None:
        try:
            return self._pipeline.exchange_rates.to_rupees(
                extraction.currency, extraction.invoice_date
            )
        except ExchangeRateUnavailable as unavailable:
            self._warnings.append(f"{extraction.vendor} {extraction.invoice_date}: {unavailable}")
            return None

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

    def _read(self, pdf: bytes, identity: str) -> tuple[Extraction, list[Doubt], bool]:
        """What the document says, the doubts about it, and whether it was read again."""
        threshold = self._settings.anomaly_threshold
        extraction = _as_charged(self._pipeline.extractor.extract(pdf))
        reading = reading_doubts(extraction, self._email)
        read_again = False
        stronger = self._pipeline.stronger_extractor
        # Only a doubt about the reading is worth a second reading. A total far from
        # the usual one was read correctly as far as anyone knows.
        if reading and stronger is not None:
            try:
                extraction = _as_charged(stronger.extract(pdf))
            except (ExtractionFailed, NotABillingDocument):
                pass  # The first reading stands, with its doubts.
            else:
                read_again = True
                reading = reading_doubts(extraction, self._email)
        history = history_doubts(extraction, self._history(extraction, identity), threshold)
        return extraction, reading + history, read_again

    def _document(self, found: _Found) -> CollectedDocument | PendingDocument | CollectionMonth:
        """The billing document, or the other collection month it belongs to.

        Raises PortalFetchFailed, RenderFailed, _ManualDownloadNeeded, NotABillingDocument
        or ExtractionFailed.
        """
        ledger = self._pipeline.ledger
        held = ledger.pending_with(found.identity)
        if held is not None:
            # Waiting for a person. It is not read again until they have decided.
            return held

        doubts: list[Doubt] = []
        read_again = False
        known = ledger.document_with(found.identity)
        if known is not None:
            pdf = None
            extraction, link, rate = known.extraction, known.file_link, known.inr_rate
        else:
            produced = found.produce()
            if isinstance(produced, LoginGated):
                raise _ManualDownloadNeeded
            pdf = produced
            extraction, doubts, read_again = self._read(pdf, found.identity)
            link, rate = "", None

        if not self._month.contains(extraction.invoice_date):
            return CollectionMonth.of(extraction.invoice_date)
        if rate is None:
            rate = self._rate(extraction)
        if doubts and pdf is not None:
            folder = f"{self._month}/pending"
            link = self._pipeline.archive.save(folder, filename(extraction), pdf)
            return PendingDocument(
                found.identity, extraction, link, tuple(doubts), rate, read_again
            )
        if pdf is not None:
            link = self._pipeline.archive.save(str(self._month), filename(extraction), pdf)
        return CollectedDocument(found.identity, extraction, link, rate)

    def _collect(self) -> None:
        found = self._found()
        if found is None:
            return

        documents: list[CollectedDocument] = []
        pending: list[PendingDocument] = []
        other_months: list[CollectionMonth] = []
        try:
            for each in found:
                document = self._document(each)
                if isinstance(document, CollectionMonth):
                    other_months.append(document)
                elif isinstance(document, PendingDocument):
                    pending.append(document)
                else:
                    documents.append(document)
        except _ManualDownloadNeeded:
            self._record(EmailState.NEEDS_REVIEW, "manual download needed")
            return
        except NotABillingDocument as finding:
            self._record(EmailState.SKIPPED, str(finding))
            return
        except (PortalFetchFailed, RenderFailed, ExtractionFailed) as failure:
            self._record(EmailState.FAILED, str(failure))
            return

        if pending:
            # The whole email waits, so the documents that raised no doubt wait with it.
            waiting = (
                *pending,
                *(
                    PendingDocument(d.content_hash, d.extraction, d.file_link, (), d.inr_rate)
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
) -> RunResult:
    settings = settings or Settings()
    window = timedelta(days=settings.search_window_days)
    warnings: list[str] = []
    ledger = pipeline.ledger
    failed_source_accounts: dict[str, str] = {}
    for source in sources:
        try:
            emails = source.emails_between(month.start - window, month.end + window)
        except SourceAccountUnavailable as unavailable:
            # The other source accounts are still read.
            failed_source_accounts[source.source_account] = str(unavailable)
            ledger.record_sync(month, source.source_account, reason=str(unavailable))
            continue
        ledger.record_sync(month, source.source_account)
        for email in emails:
            _Examination(month, email, pipeline, warnings, settings).run()

    summary = summarise(ledger.documents(month))
    suggestions = _suggest_vendors(ledger)
    reconciliation = reconcile_month(ledger, month, summary)
    for writer in summary_writers:
        writer.write(summary)
    return RunResult(
        summary=summary,
        warnings=warnings,
        gaps=reconciliation.gaps,
        upcoming=reconciliation.upcoming,
        failed_source_accounts=failed_source_accounts,
        suggested_vendors=suggestions,
        pending=ledger.pending(month),
    )


def _suggest_vendors(ledger: Ledger) -> list[ExpectedVendor]:
    """Adds vendors that have billed the company and are on no list, as suggestions."""
    charges = [row for month in ledger.months() for row in summarise(ledger.documents(month))]
    suggestions = suggested_vendors(ledger.expected_vendors(), charges)
    for vendor in suggestions:
        ledger.save_expected_vendor(vendor)
    return [v for v in ledger.expected_vendors() if v.status == "suggested"]


def seed_expected_vendors(ledger: Ledger, vendors: Sequence[ExpectedVendor]) -> None:
    """Fills the expected vendor list on first run. A list that has entries is left alone."""
    if not ledger.expected_vendors():
        for vendor in vendors:
            ledger.save_expected_vendor(vendor)
