"""Turns the billing documents of a month into one summary row per charge.

Two things make several documents one charge: the same document found in more than
one source account, and an invoice and a receipt issued for the same movement of money.
"""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import timedelta

from invoice_collector.domain import SummaryRow
from invoice_collector.ledger import DocumentRecord

# An invoice and its receipt are rarely issued on the same day.
PAIRING_WINDOW = timedelta(days=10)


@dataclass(frozen=True)
class _Document:
    row: SummaryRow
    paired: bool = False


def _one_per_document(records: Sequence[DocumentRecord]) -> list[SummaryRow]:
    by_hash: dict[str, list[DocumentRecord]] = {}
    for record in records:
        by_hash.setdefault(record.content_hash, []).append(record)

    rows: list[SummaryRow] = []
    for copies in by_hash.values():
        first = copies[0]
        rows.append(
            SummaryRow(
                vendor=first.extraction.vendor,
                document_type=first.extraction.document_type,
                invoice_date=first.extraction.invoice_date,
                total=first.extraction.total,
                currency=first.extraction.currency,
                source_accounts=tuple(sorted({copy.source_account for copy in copies})),
                file_link=first.file_link,
                inr_rate=first.inr_rate,
            )
        )
    return rows


def _same_charge(invoice: SummaryRow, receipt: SummaryRow) -> bool:
    return (
        invoice.vendor.casefold() == receipt.vendor.casefold()
        and invoice.total == receipt.total
        and invoice.currency == receipt.currency
        and abs(invoice.invoice_date - receipt.invoice_date) <= PAIRING_WINDOW
    )


def _pair_receipts_with_invoices(rows: list[SummaryRow]) -> list[SummaryRow]:
    invoices = [row for row in rows if row.document_type == "invoice"]
    receipts = [row for row in rows if row.document_type == "receipt"]
    others = [row for row in rows if row.document_type not in ("invoice", "receipt")]

    charges: list[SummaryRow] = []
    for invoice in invoices:
        receipt = next((r for r in receipts if _same_charge(invoice, r)), None)
        if receipt is None:
            charges.append(invoice)
            continue
        receipts.remove(receipt)
        accounts = tuple(sorted({*invoice.source_accounts, *receipt.source_accounts}))
        note = f"receipt also received: {receipt.file_link}"
        charges.append(replace(invoice, source_accounts=accounts, notes=note))
    return charges + receipts + others


def summarise(records: Sequence[DocumentRecord]) -> list[SummaryRow]:
    charges = _pair_receipts_with_invoices(_one_per_document(records))
    return sorted(charges, key=lambda row: (row.invoice_date, row.vendor.casefold(), row.file_link))
