"""What the Summary screen shows for one collection month."""

from collections import Counter
from decimal import Decimal
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from pydantic import BaseModel

from invoice_collector.charges import summarise
from invoice_collector.domain import (
    CollectionMonth,
    DocumentType,
    EmailState,
    GapKind,
    SignalKind,
    SummaryRow,
)
from invoice_collector.ledger import ExaminedEmail, Ledger
from invoice_collector.naming import NamedFields, filename
from invoice_collector.reconciler import reconcile_month


class Row(BaseModel):
    vendor: str
    document_type: DocumentType
    date: str
    amount: str
    currency: str
    source_account: str
    file_name: str
    file_url: str
    amount_inr: str | None
    inr_rate: str | None
    notes: str
    # Identifies the billing document, for its history. See document_trail.py.
    content_hash: str | None = None


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


class GapRow(BaseModel):
    vendor: str
    kind: GapKind
    source_account: str | None
    explanation: str | None


class Upcoming(BaseModel):
    vendor: str
    source_account: str
    note: str


class FailedSourceAccount(BaseModel):
    source_account: str
    reason: str | None


class MonthSummary(BaseModel):
    month: str
    rows: list[Row]
    totals: list[Total]
    total_inr: str
    rows_without_rupees: int
    counts: Counts
    needs_review: list[EmailNeedingReview]
    skipped: list[EmailWithReason]
    failed: list[EmailWithReason]
    billing_signals: list[Signal]
    gaps: list[GapRow]
    upcoming: list[Upcoming]
    failed_source_accounts: list[FailedSourceAccount]


def _is_web_link(file_link: str) -> bool:
    return file_link.startswith(("https://", "http://"))


def file_name(file_link: str) -> str:
    """The last part of a link: the file's name when the link is a path on this machine.

    For a web link, such as one to Google Drive, it is no name at all; see document_name.
    """
    return PurePosixPath(file_link.replace("\\", "/")).name


def document_name(file_link: str, fields: NamedFields) -> str:
    """The name the dashboard shows for a billing document's file.

    A link on this machine is a path, and its name is the file's. A web link, as to Google
    Drive, names nothing, so the name is the one the run gives a file from the document's
    fields. It carries no number that told it from another file of the same name; where
    the local copy can be found, its name is the better one to show.
    """
    if _is_web_link(file_link):
        return filename(fields)
    return file_name(file_link)


def held_document_route(month: CollectionMonth, content_hash: str, name: str) -> str:
    """Where the dashboard opens a held document's PDF: by the document, since two held
    documents can share a file's name (one in the pending folder, one beside it); the name
    is for the browser."""
    return (
        f"/api/months/{month}/review/billing-documents/{quote(content_hash, safe='')}/"
        f"{quote(name, safe='')}"
    )


def file_url(month: CollectionMonth, file_link: str) -> str:
    """Where the dashboard opens a billing document of the month."""
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
    documents = ledger.documents(month)
    rows = summarise(documents)
    hash_of = {d.file_link: d.content_hash for d in documents}
    emails = ledger.examined_emails(month)
    reconciliation = reconcile_month(ledger, month, rows)
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
                file_name=document_name(row.file_link, row),
                file_url=file_url(month, row.file_link),
                amount_inr=_amount(row.inr_total) if row.inr_total is not None else None,
                inr_rate=str(row.inr_rate) if row.inr_rate is not None else None,
                notes=row.notes,
                content_hash=hash_of.get(row.file_link),
            )
            for row in rows
        ],
        totals=_totals(rows),
        total_inr=_amount(sum((r.inr_total or Decimal(0) for r in rows), Decimal(0))),
        rows_without_rupees=sum(1 for r in rows if r.inr_total is None),
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
        gaps=[
            GapRow(
                vendor=gap.vendor,
                kind=gap.kind,
                source_account=gap.source_account,
                explanation=gap.explanation,
            )
            for gap in reconciliation.gaps
        ],
        upcoming=[
            Upcoming(vendor=u.vendor, source_account=u.source_account, note=u.note)
            for u in reconciliation.upcoming
        ],
        failed_source_accounts=[
            FailedSourceAccount(source_account=s.source_account, reason=s.reason)
            for s in ledger.syncs(month)
            if not s.succeeded
        ],
    )


def filed_document(ledger: Ledger, month: CollectionMonth, name: str, kept_in: Path) -> Path | None:
    """The file of a billing document of the month, if it is kept under the given folder.

    Only a file the ledger names is served, so nothing else on the disk can be asked for.
    """
    root = kept_in.resolve()
    for document in ledger.documents(month):
        link = document.file_link
        if _is_web_link(link) or file_name(link) != name:
            continue
        path = (root / link).resolve()
        if path.is_relative_to(root) and path.is_file():
            return path
    return None
