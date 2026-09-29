"""One collection for one collection month across all source accounts."""

from collections.abc import Sequence
from dataclasses import dataclass

from invoice_collector.archive import Archive
from invoice_collector.domain import Attachment, CollectionMonth, Email, EmailState, SummaryRow
from invoice_collector.extractor import ExtractionFailed, Extractor, content_hash
from invoice_collector.ledger import CollectedDocument, Ledger
from invoice_collector.mail_source import MailSource
from invoice_collector.naming import filename
from invoice_collector.summary import SummaryWriter


@dataclass(frozen=True)
class RunResult:
    summary: list[SummaryRow]


def _is_pdf(attachment: Attachment) -> bool:
    return (
        attachment.content_type == "application/pdf"
        or attachment.filename.lower().endswith(".pdf")
        or attachment.content.startswith(b"%PDF-")
    )


def _examine(
    month: CollectionMonth, email: Email, extractor: Extractor, archive: Archive, ledger: Ledger
) -> None:
    # Keyed by content, so the same PDF attached twice is one document.
    pdfs = list({content_hash(a.content): a for a in email.attachments if _is_pdf(a)}.values())
    if not pdfs:
        ledger.record(month, email, EmailState.SKIPPED, reason="no PDF attachment")
        return

    try:
        extractions = [extractor.extract(pdf.content) for pdf in pdfs]
    except ExtractionFailed as failure:
        ledger.record(month, email, EmailState.FAILED, reason=str(failure))
        return

    documents = tuple(
        CollectedDocument(
            content_hash=content_hash(pdf.content),
            extraction=extraction,
            file_link=archive.save(str(month), filename(extraction), pdf.content),
        )
        for pdf, extraction in zip(pdfs, extractions, strict=True)
    )
    ledger.record(month, email, EmailState.COLLECTED, documents=documents)


def collect(
    month: CollectionMonth,
    *,
    sources: Sequence[MailSource],
    extractor: Extractor,
    archive: Archive,
    ledger: Ledger,
    summary_writers: Sequence[SummaryWriter],
) -> RunResult:
    for source in sources:
        for email in source.emails_between(month.start, month.end):
            _examine(month, email, extractor, archive, ledger)

    summary = ledger.summary(month)
    for writer in summary_writers:
        writer.write(summary)
    return RunResult(summary)
