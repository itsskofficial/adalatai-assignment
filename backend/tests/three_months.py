"""Three collection months of charges, recorded through the ledger as a run records them.

June 2026
  AWS invoice 100 USD at 83      engineering   8,300.00
  Slack invoice 50 USD at 83     design        4,150.00
July 2026
  AWS invoice 120 USD at 84      engineering  10,080.00
  Slack invoice 50 USD at 84     design and engineering (the same document)  4,200.00
  Notion receipt 20 EUR          design       no rupee amount
August 2026
  AWS invoice 150 USD at 85      engineering  12,750.00
  Slack invoice 50 USD at 85     design        4,250.00 (its receipt is the same charge)
  Figma credit note -10 USD      design         -850.00
  Linear invoice 1000 INR at 1   ops           1,000.00 (first seen this month)
  Zoom invoice 999 USD           needs review, so not counted
"""

from datetime import UTC, date, datetime
from decimal import Decimal

from invoice_collector.domain import CollectionMonth, DocumentType, Email, EmailState, Extraction
from invoice_collector.ledger import CollectedDocument, Ledger

JUNE = CollectionMonth(2026, 6)
JULY = CollectionMonth(2026, 7)
AUGUST = CollectionMonth(2026, 8)

ENGINEERING = "engineering@nyayalabs.example"
DESIGN = "design@nyayalabs.example"
OPS = "ops@nyayalabs.example"


def _document(
    document_type: DocumentType,
    vendor: str,
    day: date,
    total: str,
    currency: str,
    rate: str | None,
) -> CollectedDocument:
    name = f"{day:%Y-%m}_{vendor}_{Decimal(total):.2f}-{currency}"
    if document_type != "invoice":
        name = f"{name}-{document_type}"
    return CollectedDocument(
        content_hash=f"hash-of-{name}",
        extraction=Extraction(document_type, vendor, day, Decimal(total), currency),
        file_link=f"archive/{day:%Y-%m}/{name}.pdf",
        inr_rate=Decimal(rate) if rate is not None else None,
    )


def _record(
    ledger: Ledger,
    month: CollectionMonth,
    account: str,
    message_id: str,
    document: CollectedDocument,
    state: EmailState = EmailState.COLLECTED,
) -> None:
    day = document.extraction.invoice_date
    ledger.record(
        month,
        Email(
            source_account=account,
            message_id=message_id,
            sender="Billing <billing@vendor.example>",
            subject=f"Your {document.extraction.vendor} {document.extraction.document_type}",
            received_at=datetime(day.year, day.month, day.day, 9, 0, tzinfo=UTC),
        ),
        state,
        reason="check the amount" if state is EmailState.NEEDS_REVIEW else None,
        documents=(document,),
    )


def record_three_months(ledger: Ledger) -> None:
    _record(
        ledger, JUNE, ENGINEERING, "m-aws-jun",
        _document("invoice", "AWS", date(2026, 6, 2), "100", "USD", "83"),
    )  # fmt: skip
    _record(
        ledger, JUNE, DESIGN, "m-slack-jun",
        _document("invoice", "Slack", date(2026, 6, 3), "50", "USD", "83"),
    )  # fmt: skip

    _record(
        ledger, JULY, ENGINEERING, "m-aws-jul",
        _document("invoice", "AWS", date(2026, 7, 2), "120", "USD", "84"),
    )  # fmt: skip
    slack_july = _document("invoice", "Slack", date(2026, 7, 3), "50", "USD", "84")
    _record(ledger, JULY, DESIGN, "m-slack-jul", slack_july)
    _record(ledger, JULY, ENGINEERING, "m-slack-jul-copy", slack_july)
    _record(
        ledger, JULY, DESIGN, "m-notion-jul",
        _document("receipt", "Notion", date(2026, 7, 9), "20", "EUR", None),
    )  # fmt: skip

    _record(
        ledger, AUGUST, ENGINEERING, "m-aws-aug",
        _document("invoice", "AWS", date(2026, 8, 2), "150", "USD", "85"),
    )  # fmt: skip
    _record(
        ledger, AUGUST, DESIGN, "m-slack-aug",
        _document("invoice", "Slack", date(2026, 8, 3), "50", "USD", "85"),
    )  # fmt: skip
    _record(
        ledger, AUGUST, DESIGN, "m-slack-aug-receipt",
        _document("receipt", "Slack", date(2026, 8, 6), "50", "USD", "85"),
    )  # fmt: skip
    _record(
        ledger, AUGUST, DESIGN, "m-figma-aug",
        _document("credit_note", "Figma", date(2026, 8, 21), "-10", "USD", "85"),
    )  # fmt: skip
    _record(
        ledger, AUGUST, OPS, "m-linear-aug",
        _document("invoice", "Linear", date(2026, 8, 12), "1000", "INR", "1"),
    )  # fmt: skip
    _record(
        ledger, AUGUST, ENGINEERING, "m-zoom-aug",
        _document("invoice", "Zoom", date(2026, 8, 14), "999", "USD", "85"),
        EmailState.NEEDS_REVIEW,
    )  # fmt: skip
