"""The fixed queries behind "Ask your invoices", as plain functions over known charges."""

from datetime import date
from decimal import Decimal

from invoice_collector.charge_history import Charge
from invoice_collector.domain import CollectionMonth, DocumentType, SummaryRow
from invoice_collector.fixed_queries import (
    Period,
    document_type_counts,
    largest_charges,
    new_vendors,
    spend_by_month,
    spend_by_vendor,
    total_spend,
    vendor_charges,
)

ENGINEERING = "engineering@nyayalabs.example"
DESIGN = "design@nyayalabs.example"

JUNE = CollectionMonth(2026, 6)
JULY = CollectionMonth(2026, 7)
AUGUST = CollectionMonth(2026, 8)


def charge(
    vendor: str,
    day: date,
    total: str,
    rate: str | None,
    *accounts: str,
    document_type: DocumentType = "invoice",
    currency: str = "USD",
) -> Charge:
    return Charge(
        CollectionMonth.of(day),
        SummaryRow(
            vendor=vendor,
            document_type=document_type,
            invoice_date=day,
            total=Decimal(total),
            currency=currency,
            source_accounts=accounts or (ENGINEERING,),
            file_link=f"archive/{vendor}-{day}.pdf",
            inr_rate=Decimal(rate) if rate is not None else None,
        ),
    )


AWS_JUNE = charge("AWS", date(2026, 6, 2), "100", "83")  # 8,300.00
SLACK_JUNE = charge("Slack", date(2026, 6, 3), "50", "83", DESIGN)  # 4,150.00
AWS_JULY = charge("AWS", date(2026, 7, 2), "120", "84")  # 10,080.00
SLACK_JULY = charge("Slack", date(2026, 7, 3), "50", "84", DESIGN, ENGINEERING)  # 4,200.00
NOTION_JULY = charge(
    "Notion", date(2026, 7, 9), "20", None, DESIGN, document_type="receipt", currency="EUR"
)
AWS_AUGUST = charge("AWS", date(2026, 8, 2), "150", "85")  # 12,750.00
FIGMA_AUGUST = charge(
    "Figma", date(2026, 8, 21), "-10", "85", DESIGN, document_type="credit_note"
)  # -850.00
LINEAR_AUGUST = charge("Linear", date(2026, 8, 12), "1000", "1", currency="INR")  # 1,000.00

CHARGES = [
    AWS_JUNE,
    SLACK_JUNE,
    AWS_JULY,
    SLACK_JULY,
    NOTION_JULY,
    AWS_AUGUST,
    FIGMA_AUGUST,
    LINEAR_AUGUST,
]
SUMMER = Period(JUNE, AUGUST)


def test_period_holds_the_months_from_first_to_last() -> None:
    assert SUMMER.contains(JULY)
    assert not SUMMER.contains(CollectionMonth(2026, 9))
    assert SUMMER.months() == [JUNE, JULY, AUGUST]


def test_total_spend_over_everything() -> None:
    total = total_spend(CHARGES)

    # 8,300 + 4,150 + 10,080 + 4,200 + 12,750 - 850 + 1,000
    assert total.inr_total == Decimal("39630.00")
    assert total.without_rupees == (NOTION_JULY,)
    assert len(total.charges) == 8


def test_total_spend_on_one_vendor_in_a_period_ignores_letter_case() -> None:
    total = total_spend(CHARGES, vendor="aws", period=Period(JULY, AUGUST))

    assert total.inr_total == Decimal("22830.00")
    assert total.charges == (AWS_JULY, AWS_AUGUST)


def test_total_spend_of_a_source_account_counts_a_shared_charge_under_the_first() -> None:
    design = total_spend(CHARGES, source_account=DESIGN)
    engineering = total_spend(CHARGES, source_account=ENGINEERING)

    # Slack in June and July, and the Figma credit note: 4,150 + 4,200 - 850
    assert design.inr_total == Decimal("7500.00")
    assert engineering.inr_total == Decimal("32130.00")
    assert SLACK_JULY in design.charges
    assert SLACK_JULY not in engineering.charges


def test_spend_by_vendor_is_ranked_largest_first() -> None:
    ranked = spend_by_vendor(CHARGES, SUMMER)

    assert [(each.key, each.inr_total) for each in ranked] == [
        ("AWS", Decimal("31130.00")),
        ("Slack", Decimal("8350.00")),
        ("Linear", Decimal("1000.00")),
        ("Figma", Decimal("-850.00")),
    ]
    assert ranked[0].charges == (AWS_JUNE, AWS_JULY, AWS_AUGUST)


def test_spend_by_month_for_a_vendor_includes_months_without_charges() -> None:
    months = spend_by_month(CHARGES, "Linear", SUMMER)

    assert [(each.key, each.inr_total) for each in months] == [
        ("2026-06", Decimal("0.00")),
        ("2026-07", Decimal("0.00")),
        ("2026-08", Decimal("1000.00")),
    ]


def test_largest_charges_in_a_period() -> None:
    largest = largest_charges(CHARGES, SUMMER, limit=3)

    assert largest == (AWS_AUGUST, AWS_JULY, AWS_JUNE)


def test_largest_charges_leave_out_charges_without_a_rupee_amount() -> None:
    assert NOTION_JULY not in largest_charges(CHARGES, Period(JULY, JULY), limit=10)


def test_charges_from_a_vendor_in_a_period_by_date() -> None:
    assert vendor_charges(CHARGES, "Slack", Period(JULY, AUGUST)) == (SLACK_JULY,)


def test_vendors_first_seen_in_a_period() -> None:
    found = new_vendors(CHARGES, Period(AUGUST, AUGUST))

    assert [(each.vendor, each.first_month) for each in found] == [
        ("Figma", AUGUST),
        ("Linear", AUGUST),
    ]
    assert found[1].charges == (LINEAR_AUGUST,)


def test_a_vendor_seen_before_the_period_is_not_new() -> None:
    assert [each.vendor for each in new_vendors(CHARGES, Period(JULY, AUGUST))] == [
        "Figma",
        "Linear",
        "Notion",
    ]


def test_count_of_billing_documents_by_document_type() -> None:
    counts = document_type_counts(CHARGES, SUMMER)

    assert [(each.document_type, each.count) for each in counts] == [
        ("invoice", 6),
        ("receipt", 1),
        ("credit_note", 1),
    ]
    assert counts[2].charges == (FIGMA_AUGUST,)
