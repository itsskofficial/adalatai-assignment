"""The vocabulary of CONTEXT.md as types."""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

DocumentType = Literal["invoice", "receipt", "credit_note"]


class EmailState(StrEnum):
    COLLECTED = "collected"
    NEEDS_REVIEW = "needs_review"
    SKIPPED = "skipped"
    FAILED = "failed"


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


@dataclass(frozen=True)
class Extraction:
    document_type: DocumentType
    vendor: str
    invoice_date: date
    total: Decimal
    currency: str


@dataclass(frozen=True)
class SummaryRow:
    vendor: str
    invoice_date: date
    total: Decimal
    currency: str
    source_account: str
    file_link: str
