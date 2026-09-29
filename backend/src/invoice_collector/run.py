"""One collection for one collection month across all source accounts."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import timedelta
from decimal import Decimal

from invoice_collector.archive import Archive
from invoice_collector.charges import summarise
from invoice_collector.classifier import ClassificationFailed, Classifier
from invoice_collector.domain import (
    BillingSignal,
    CollectionMonth,
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
from invoice_collector.ledger import CollectedDocument, Ledger
from invoice_collector.mail_source import MailSource, SourceAccountUnavailable
from invoice_collector.naming import filename
from invoice_collector.portal import LoginGated, PortalFetcher, PortalFetchFailed
from invoice_collector.reconciler import reconcile_month, suggested_vendors
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


@dataclass(frozen=True)
class Settings:
    # An invoice is often emailed a day or two before or after the date printed on it.
    search_window_days: int = 7


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
        self, month: CollectionMonth, email: Email, pipeline: Pipeline, warnings: list[str]
    ) -> None:
        self._month = month
        self._email = email
        self._pipeline = pipeline
        self._warnings = warnings
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
        signal: BillingSignal | None = None,
    ) -> None:
        # An email with nothing collected from it belongs to the month it arrived in.
        # One that arrived outside the month is left for that month's run.
        if not documents and not self._month.contains(self._email.received_at):
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

    def _document(self, found: _Found) -> CollectedDocument | CollectionMonth:
        """The billing document, or the other collection month it belongs to.

        Raises PortalFetchFailed, RenderFailed, _ManualDownloadNeeded, NotABillingDocument
        or ExtractionFailed.
        """
        known = self._pipeline.ledger.document_with(found.identity)
        if known is not None:
            pdf = None
            extraction, link, rate = known.extraction, known.file_link, known.inr_rate
        else:
            produced = found.produce()
            if isinstance(produced, LoginGated):
                raise _ManualDownloadNeeded
            pdf = produced
            extraction = _as_charged(self._pipeline.extractor.extract(pdf))
            link, rate = "", None

        if not self._month.contains(extraction.invoice_date):
            return CollectionMonth.of(extraction.invoice_date)
        if pdf is not None:
            link = self._pipeline.archive.save(str(self._month), filename(extraction), pdf)
        if rate is None:
            rate = self._rate(extraction)
        return CollectedDocument(found.identity, extraction, link, rate)

    def _collect(self) -> None:
        found = self._found()
        if found is None:
            return

        documents: list[CollectedDocument] = []
        other_months: list[CollectionMonth] = []
        try:
            for each in found:
                document = self._document(each)
                if isinstance(document, CollectionMonth):
                    other_months.append(document)
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

        if documents:
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
    window = timedelta(days=(settings or Settings()).search_window_days)
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
            _Examination(month, email, pipeline, warnings).run()

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
