"""The vocabulary of CONTEXT.md as types."""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

DocumentType = Literal["invoice", "receipt", "credit_note"]
Confidence = Literal["high", "medium", "low"]
SignalKind = Literal["payment_failed", "renewal_reminder"]
EmailKind = Literal[
    "invoice", "receipt", "credit_note", "payment_failed", "renewal_reminder", "not_billing"
]


class InvoiceFormat(StrEnum):
    ATTACHMENT = "attachment"
    BODY = "body"
    PORTAL_LINK = "portal_link"


class EmailState(StrEnum):
    COLLECTED = "collected"
    NEEDS_REVIEW = "needs_review"
    SKIPPED = "skipped"
    FAILED = "failed"


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


@dataclass(frozen=True)
class Attachment:
    filename: str
    content_type: str
    content: bytes


@dataclass(frozen=True)
class Email:
    source_account: str
    message_id: str
    sender: str
    subject: str
    received_at: datetime
    attachments: tuple[Attachment, ...] = ()
    html_body: str | None = None
    text_body: str | None = None


@dataclass(frozen=True)
class Extraction:
    document_type: DocumentType
    vendor: str
    invoice_date: date
    total: Decimal
    currency: str
    confidence: Confidence = "high"
    doubts: str = ""


@dataclass(frozen=True)
class Classification:
    kind: EmailKind
    vendor: str | None
    confidence: Confidence


@dataclass(frozen=True)
class BillingSignal:
    kind: SignalKind
    vendor: str | None
    source_account: str
    message_id: str
    subject: str
    received_at: datetime


@dataclass(frozen=True)
class SummaryRow:
    vendor: str
    document_type: DocumentType
    invoice_date: date
    total: Decimal
    currency: str
    source_account: str
    file_link: str
