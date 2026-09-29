"""Charges across collection months, read from the ledger without changing it.

Each month is summarised on its own with charges.summarise, so a billing document found in
several source accounts, and an invoice with its receipt, are each counted once.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from invoice_collector.charges import summarise
from invoice_collector.domain import CollectionMonth, DocumentType, SummaryRow
from invoice_collector.ledger import Ledger


@dataclass(frozen=True)
class Charge:
    """One charge and the collection month it belongs to."""

    collection_month: CollectionMonth
    row: SummaryRow

    @property
    def month(self) -> str:
        return str(self.collection_month)

    @property
    def vendor(self) -> str:
        return self.row.vendor

    @property
    def document_type(self) -> DocumentType:
        return self.row.document_type

    @property
    def invoice_date(self) -> date:
        return self.row.invoice_date

    @property
    def total(self) -> Decimal:
        return self.row.total

    @property
    def currency(self) -> str:
        return self.row.currency

    @property
    def inr_total(self) -> Decimal | None:
        return self.row.inr_total

    @property
    def source_accounts(self) -> tuple[str, ...]:
        return self.row.source_accounts

    @property
    def first_source_account(self) -> str:
        """The source account a charge found in several is attributed to."""
        return self.row.source_accounts[0] if self.row.source_accounts else ""

    @property
    def file_link(self) -> str:
        return self.row.file_link


def charges_in(ledger: Ledger, months: Iterable[CollectionMonth]) -> list[Charge]:
    """Every charge of the given collection months, month by month."""
    return [
        Charge(month, row)
        for month in sorted(set(months), key=str)
        for row in summarise(ledger.documents(month))
    ]


def month_before(month: CollectionMonth) -> CollectionMonth:
    if month.month == 1:
        return CollectionMonth(month.year - 1, 12)
    return CollectionMonth(month.year, month.month - 1)


def month_after(month: CollectionMonth) -> CollectionMonth:
    if month.month == 12:
        return CollectionMonth(month.year + 1, 1)
    return CollectionMonth(month.year, month.month + 1)


def months_from(first: CollectionMonth, last: CollectionMonth) -> list[CollectionMonth]:
    """Every calendar month from the first to the last, both included."""
    months: list[CollectionMonth] = []
    month = first
    while str(month) <= str(last):
        months.append(month)
        month = month_after(month)
    return months
