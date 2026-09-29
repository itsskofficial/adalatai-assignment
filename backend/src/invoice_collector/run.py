"""One collection for one collection month across all source accounts."""

from collections.abc import Sequence
from dataclasses import dataclass

from invoice_collector.archive import Archive
from invoice_collector.domain import CollectionMonth, Email, EmailState, SummaryRow
from invoice_collector.extractor import ExtractionFailed, Extractor, content_hash
from invoice_collector.ledger import CollectedDocument, Ledger
from invoice_collector.mail_source import MailSource
from invoice_collector.naming import filename
from invoice_collector.portal import LoginGated, PortalFetcher, PortalFetchFailed
from invoice_collector.renderer import Renderer
from invoice_collector.routing import Attachments, Body, NotBilling, PortalLink, route
from invoice_collector.summary import SummaryWriter


@dataclass(frozen=True)
class RunResult:
    summary: list[SummaryRow]


@dataclass(frozen=True)
class Pipeline:
    """The modules a run is composed from."""

    extractor: Extractor
    renderer: Renderer
    portal_fetcher: PortalFetcher
    archive: Archive
    ledger: Ledger


def _examine(month: CollectionMonth, email: Email, pipeline: Pipeline) -> None:
    ledger = pipeline.ledger
    routed = route(email)

    match routed:
        case NotBilling(reason):
            ledger.record(month, email, EmailState.SKIPPED, reason=reason)
            return
        case Attachments(attachments):
            # Keyed by content, so the same PDF attached twice is one document.
            pdfs = list({content_hash(a.content): a.content for a in attachments}.values())
        case Body(html):
            pdfs = [pipeline.renderer.render_html(html)]
        case PortalLink(url):
            try:
                fetched = pipeline.portal_fetcher.fetch(url)
            except PortalFetchFailed as failure:
                ledger.record(
                    month,
                    email,
                    EmailState.FAILED,
                    reason=str(failure),
                    invoice_format=routed.invoice_format,
                    portal_link=url,
                )
                return
            if isinstance(fetched, LoginGated):
                ledger.record(
                    month,
                    email,
                    EmailState.NEEDS_REVIEW,
                    reason="manual download needed",
                    invoice_format=routed.invoice_format,
                    portal_link=url,
                )
                return
            pdfs = [fetched]

    portal_link = routed.url if isinstance(routed, PortalLink) else None
    try:
        extractions = [pipeline.extractor.extract(pdf) for pdf in pdfs]
    except ExtractionFailed as failure:
        ledger.record(
            month,
            email,
            EmailState.FAILED,
            reason=str(failure),
            invoice_format=routed.invoice_format,
            portal_link=portal_link,
        )
        return

    documents = tuple(
        CollectedDocument(
            content_hash=content_hash(pdf),
            extraction=extraction,
            file_link=pipeline.archive.save(str(month), filename(extraction), pdf),
        )
        for pdf, extraction in zip(pdfs, extractions, strict=True)
    )
    ledger.record(
        month,
        email,
        EmailState.COLLECTED,
        invoice_format=routed.invoice_format,
        portal_link=portal_link,
        documents=documents,
    )


def collect(
    month: CollectionMonth,
    *,
    sources: Sequence[MailSource],
    pipeline: Pipeline,
    summary_writers: Sequence[SummaryWriter],
) -> RunResult:
    for source in sources:
        for email in source.emails_between(month.start, month.end):
            _examine(month, email, pipeline)

    summary = pipeline.ledger.summary(month)
    for writer in summary_writers:
        writer.write(summary)
    return RunResult(summary)
