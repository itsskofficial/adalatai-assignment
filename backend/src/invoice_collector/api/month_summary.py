"""What the Summary screen shows for one collection month."""

from collections import Counter
from decimal import Decimal
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from pydantic import BaseModel

from invoice_collector.domain import (
    CollectionMonth,
    DocumentType,
    EmailState,
    SignalKind,
    SummaryRow,
)
from invoice_collector.ledger import ExaminedEmail, Ledger


class Row(BaseModel):
    vendor: str
    document_type: DocumentType
    date: str
    amount: str
    currency: str
    source_account: str
    file_name: str
    file_url: str


class Total(BaseModel):
    currency: str
    amount: str


class Counts(BaseModel):
    collected: int
    needs_review: int
    skipped: int
    failed: int


class EmailWithReason(BaseModel):
    source_account: str
    message_id: str
    subject: str
    reason: str | None


class EmailNeedingReview(EmailWithReason):
    portal_link: str | None


class Signal(BaseModel):
    kind: SignalKind
    vendor: str | None
    source_account: str
    message_id: str
    subject: str
    received_at: str


class MonthSummary(BaseModel):
    month: str
    rows: list[Row]
    totals: list[Total]
    counts: Counts
    needs_review: list[EmailNeedingReview]
    skipped: list[EmailWithReason]
    failed: list[EmailWithReason]
    billing_signals: list[Signal]


def _is_web_link(file_link: str) -> bool:
    return file_link.startswith(("https://", "http://"))


def file_name(file_link: str) -> str:
    return PurePosixPath(file_link.replace("\\", "/")).name


def _file_url(month: CollectionMonth, file_link: str) -> str:
    if _is_web_link(file_link):
        return file_link
    return f"/api/months/{month}/billing-documents/{quote(file_name(file_link), safe='')}"


def _amount(value: Decimal) -> str:
    return f"{value:.2f}"


def _with_reason(email: ExaminedEmail) -> EmailWithReason:
    return EmailWithReason(
        source_account=email.source_account,
        message_id=email.message_id,
        subject=email.subject,
        reason=email.reason,
    )


def _totals(rows: list[SummaryRow]) -> list[Total]:
    totals: dict[str, Decimal] = {}
    for row in rows:
        totals[row.currency] = totals.get(row.currency, Decimal(0)) + row.total
    return [
        Total(currency=currency, amount=_amount(totals[currency])) for currency in sorted(totals)
    ]


def month_summary(ledger: Ledger, month: CollectionMonth) -> MonthSummary:
    rows = ledger.summary(month)
    emails = ledger.examined_emails(month)
    states = Counter(email.state for email in emails)
    return MonthSummary(
        month=str(month),
        rows=[
            Row(
                vendor=row.vendor,
                document_type=row.document_type,
                date=row.invoice_date.isoformat(),
                amount=_amount(row.total),
                currency=row.currency,
                source_account=row.source_account,
                file_name=file_name(row.file_link),
                file_url=_file_url(month, row.file_link),
            )
            for row in rows
        ],
        totals=_totals(rows),
        counts=Counts(
            collected=states[EmailState.COLLECTED],
            needs_review=states[EmailState.NEEDS_REVIEW],
            skipped=states[EmailState.SKIPPED],
            failed=states[EmailState.FAILED],
        ),
        needs_review=[
            EmailNeedingReview(**_with_reason(email).model_dump(), portal_link=email.portal_link)
            for email in emails
            if email.state is EmailState.NEEDS_REVIEW
        ],
        skipped=[_with_reason(e) for e in emails if e.state is EmailState.SKIPPED],
        failed=[_with_reason(e) for e in emails if e.state is EmailState.FAILED],
        billing_signals=[
            Signal(
                kind=signal.kind,
                vendor=signal.vendor,
                source_account=signal.source_account,
                message_id=signal.message_id,
                subject=signal.subject,
                received_at=signal.received_at.isoformat(),
            )
            for signal in ledger.billing_signals(month)
        ],
    )


def filed_document(ledger: Ledger, month: CollectionMonth, name: str, kept_in: Path) -> Path | None:
    """The file behind a summary row of the month, if it is kept under the given folder.

    Only a file the ledger names is served, so nothing else on the disk can be asked for.
    """
    root = kept_in.resolve()
    for row in ledger.summary(month):
        if _is_web_link(row.file_link) or file_name(row.file_link) != name:
            continue
        path = (root / row.file_link).resolve()
        if path.is_relative_to(root) and path.is_file():
            return path
    return None
