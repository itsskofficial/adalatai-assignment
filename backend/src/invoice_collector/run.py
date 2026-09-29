"""One collection for one collection month across all source accounts."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from invoice_collector.archive import Archive
from invoice_collector.domain import Attachment, Email, EmailState, SummaryRow
from invoice_collector.extractor import ExtractionFailed, Extractor
from invoice_collector.ledger import Ledger
from invoice_collector.mail_source import MailSource
from invoice_collector.naming import filename
from invoice_collector.summary import SummaryWriter


@dataclass(frozen=True)
class CollectionMonth:
    year: int
    month: int

    @classmethod
    def parse(cls, text: str) -> "CollectionMonth":
        parsed = datetime.strptime(text, "%Y-%m")
        return cls(parsed.year, parsed.month)

    @property
    def start(self) -> datetime:
        return datetime(self.year, self.month, 1, tzinfo=UTC)

    @property
    def end(self) -> datetime:
        if self.month == 12:
            return datetime(self.year + 1, 1, 1, tzinfo=UTC)
        return datetime(self.year, self.month + 1, 1, tzinfo=UTC)

    def __str__(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"


@dataclass
class RunResult:
    month: CollectionMonth
    summary: list[SummaryRow] = field(default_factory=list[SummaryRow])


def _pdf_attachment(email: Email) -> Attachment | None:
    for attachment in email.attachments:
        if attachment.content_type == "application/pdf":
            return attachment
    return None


def collect(
    month: CollectionMonth,
    *,
    sources: Sequence[MailSource],
    extractor: Extractor,
    archive: Archive,
    ledger: Ledger,
    summary_writers: Sequence[SummaryWriter],
) -> RunResult:
    result = RunResult(month)
    for source in sources:
        for email in source.emails_between(month.start, month.end):
            attachment = _pdf_attachment(email)
            if attachment is None:
                ledger.record_email(email, EmailState.SKIPPED, "no PDF attachment")
                continue

            try:
                extraction = extractor.extract(attachment.content)
            except ExtractionFailed as failure:
                ledger.record_email(email, EmailState.FAILED, str(failure))
                continue

            link = archive.save(str(month), filename(extraction), attachment.content)
            ledger.record_email(email, EmailState.COLLECTED)
            ledger.record_billing_document(email, extraction, link)
            result.summary.append(
                SummaryRow(
                    vendor=extraction.vendor,
                    invoice_date=extraction.invoice_date,
                    total=extraction.total,
                    currency=extraction.currency,
                    source_account=email.source_account,
                    file_link=link,
                )
            )

    for writer in summary_writers:
        writer.write(result.summary)
    return result
