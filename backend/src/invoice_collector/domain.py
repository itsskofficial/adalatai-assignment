"""The vocabulary of CONTEXT.md as types."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Literal

DocumentType = Literal["invoice", "receipt", "credit_note"]
Confidence = Literal["high", "medium", "low"]
SignalKind = Literal["payment_failed", "renewal_reminder"]
BillingCycle = Literal["monthly", "annual"]
VendorStatus = Literal["expected", "suggested", "ignored"]
GapKind = Literal["missing", "unknown"]
Field = Literal["vendor", "invoice_date", "total", "currency", "document_type"]
# How a run was started: on the schedule, from the dashboard, or at the command line.
StartedBy = Literal["schedule", "dashboard", "command_line"]
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

    @classmethod
    def of(cls, day: date) -> "CollectionMonth":
        return cls(day.year, day.month)

    def contains(self, day: date) -> bool:
        """Whether a date, or the date of a moment, falls in this month."""
        return (day.year, day.month) == (self.year, self.month)

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
    # Read from the document when it states them. Used to check the total.
    subtotal: Decimal | None = None
    tax: Decimal | None = None
    # What read the document: a model's name, or "rules". None when it is not known.
    by: str | None = None


@dataclass(frozen=True)
class Doubt:
    """A reason not to trust what was read. The field is the one to look at, if one is."""

    field: Field | None
    reason: str


@dataclass(frozen=True)
class Classification:
    kind: EmailKind
    vendor: str | None
    confidence: Confidence
    probability: float | None = None
    # What classified the email: a model's name, or "rules". None when it is not known.
    by: str | None = None


@dataclass(frozen=True)
class BillingSignal:
    kind: SignalKind
    vendor: str | None
    source_account: str
    message_id: str
    subject: str
    received_at: datetime


@dataclass(frozen=True)
class ExpectedVendor:
    vendor: str
    source_account: str | None
    billing_cycle: BillingCycle
    renewal_month: int | None
    usual_amount: Decimal | None
    currency: str | None
    status: VendorStatus = "expected"


@dataclass(frozen=True)
class Gap:
    vendor: str
    kind: GapKind
    source_account: str | None
    explanation: str | None


@dataclass(frozen=True)
class UpcomingCharge:
    vendor: str
    source_account: str
    note: str


@dataclass(frozen=True)
class Sync:
    """Whether a source account could be read for a collection month."""

    source_account: str
    succeeded: bool
    reason: str | None


@dataclass(frozen=True)
class ModelUsage:
    """The calls a run made to one model, the tokens they took, and what they cost."""

    model: str
    calls: int
    input_tokens: int
    output_tokens: int
    # None when the model's price is not known, or a call did not report its tokens.
    cost_usd: Decimal | None


def total_cost(models: Sequence[ModelUsage]) -> Decimal | None:
    """What every model cost together: zero when none was called, None when the cost of
    any is unknown."""
    costs = [m.cost_usd for m in models]
    if any(cost is None for cost in costs):
        return None
    return sum((cost for cost in costs if cost is not None), Decimal(0))


@dataclass(frozen=True)
class Run:
    """One collection for one collection month, as the ledger records it."""

    id: int
    collection_month: CollectionMonth
    started_by: StartedBy
    started_at: datetime
    # None while the run is going, and for good when it crashed.
    finished_at: datetime | None
    # The outcome of each email the run examined. None until it finishes.
    collected: int | None
    needs_review: int | None
    skipped: int | None
    failed: int | None
    # What the models the run called cost, when that is known. Zero for a run that called
    # none; None for a run whose calls were not metered, or one whose cost is unknown.
    model_cost_usd: Decimal | None
    # Each source account the run read, and whether it could.
    source_accounts: tuple[Sync, ...]
    # The calls to each model, when the run metered them. Empty for a run that called no
    # model or was not metered, which model_cost_usd tells apart.
    models: tuple[ModelUsage, ...] = ()

    @property
    def duration(self) -> timedelta | None:
        return None if self.finished_at is None else self.finished_at - self.started_at

    @property
    def failed_source_accounts(self) -> tuple[Sync, ...]:
        return tuple(s for s in self.source_accounts if not s.succeeded)


@dataclass(frozen=True)
class SummaryRow:
    vendor: str
    document_type: DocumentType
    invoice_date: date
    total: Decimal
    currency: str
    source_accounts: tuple[str, ...]
    file_link: str
    inr_rate: Decimal | None = None
    notes: str = ""

    @property
    def source_account(self) -> str:
        """Every source account the billing document was found in, as one text."""
        return "; ".join(self.source_accounts)

    @property
    def inr_total(self) -> Decimal | None:
        if self.inr_rate is None:
            return None
        return (self.total * self.inr_rate).quantize(Decimal("0.01"))
