"""Tests at the run seam: expected vendors, gaps, and source accounts that cannot be read."""

from dataclasses import replace
from datetime import UTC, date, datetime

from support import AUGUST, ENGINEERING, FINANCE, JULY, OPS, Collection, invoice_email, notice, usd

from invoice_collector.domain import Email, ExpectedVendor, Gap
from invoice_collector.run import seed_expected_vendors


def august(day: int) -> datetime:
    return datetime(2026, 8, day, 9, 0, tzinfo=UTC)


def slack_invoice(collection: Collection) -> list[Email]:
    pdf = b"%PDF-1.7 slack august"
    collection.answers[pdf] = usd("Slack", date(2026, 8, 3), "652.50")
    return [invoice_email("Slack", pdf, received=august(3))]


# Gaps


def test_expected_vendor_with_no_billing_document_is_a_gap(collection: Collection) -> None:
    collection.expect("Slack")
    collection.expect("Zoom", OPS)

    result = collection.run(slack_invoice(collection))

    assert result.gaps == [Gap("Zoom", "missing", OPS, None)]


def test_month_in_which_every_expected_vendor_billed_has_no_gaps(collection: Collection) -> None:
    collection.expect("Slack")

    result = collection.run(slack_invoice(collection))

    assert result.gaps == []


def test_vendor_is_recognised_whatever_its_legal_form(collection: Collection) -> None:
    collection.expect("SLACK, Inc.")

    result = collection.run(slack_invoice(collection))

    assert result.gaps == []


def test_vendors_whose_names_differ_by_a_word_are_two_vendors(collection: Collection) -> None:
    collection.expect("Slack Labs")
    collection.expect("Slack Systems")

    result = collection.run(slack_invoice(collection))

    assert [gap.vendor for gap in result.gaps] == ["Slack Labs", "Slack Systems"]
    assert [v.vendor for v in result.suggested_vendors] == ["Slack"]


def test_vendor_billed_annually_is_expected_only_in_its_renewal_month(
    collection: Collection,
) -> None:
    collection.expect("1Password", cycle="annual", renewal_month=11)
    collection.expect("GitHub", cycle="annual", renewal_month=8)

    result = collection.run([])

    assert [gap.vendor for gap in result.gaps] == ["GitHub"]


def test_gap_is_explained_by_a_payment_that_failed(collection: Collection) -> None:
    collection.expect("Zoom", OPS)
    failed = notice(
        "Zoom",
        "Your payment failed",
        "We could not process your payment of $149.90.",
        received=august(12),
        account=OPS,
    )

    result = collection.run([failed])

    assert result.gaps == [Gap("Zoom", "missing", OPS, "payment failed on 12 August")]


def datadog_invoice(collection: Collection, total: str) -> list[Email]:
    pdf = b"%PDF-1.7 datadog august"
    collection.answers[pdf] = usd("Datadog", date(2026, 8, 5), total)
    return [invoice_email("Datadog", pdf, received=august(5))]


def test_gap_is_explained_by_a_document_held_for_review(collection: Collection) -> None:
    collection.expect("Datadog", usual="1000.00")

    result = collection.run(datadog_invoice(collection, "1390.00"))

    assert result.gaps == [
        Gap(
            "Datadog",
            "missing",
            ENGINEERING,
            "held for review: the total is 39% above the usual 1000.00 USD for Datadog",
        )
    ]


def test_held_document_and_a_failed_payment_both_explain_the_gap(
    collection: Collection,
) -> None:
    collection.expect("Datadog", usual="1000.00")
    failed = notice(
        "Datadog",
        "Your payment failed",
        "We could not process your payment of $1,390.00.",
        received=august(20),
    )

    result = collection.run([*datadog_invoice(collection, "1390.00"), failed])

    [gap] = result.gaps
    assert gap.explanation == (
        "held for review: the total is 39% above the usual 1000.00 USD for Datadog; "
        "payment failed on 20 August"
    )


def test_gap_of_an_unread_source_account_says_a_document_is_held_from_another(
    collection: Collection,
) -> None:
    collection.expect("Datadog", None, usual="1000.00")
    collection.unavailable[OPS] = "sign-in expired"

    result = collection.run(datadog_invoice(collection, "1390.00"))

    [gap] = result.gaps
    assert (gap.kind, gap.explanation) == (
        "unknown",
        f"{OPS} could not be read; held for review: the total is 39% above the usual "
        "1000.00 USD for Datadog",
    )


def test_held_credit_note_does_not_explain_a_gap(collection: Collection) -> None:
    collection.expect("Slack")
    pdf = b"%PDF-1.7 slack credit"
    collection.answers[pdf] = replace(
        usd("Slack", date(2026, 8, 18), "45.00", "credit_note"), confidence="low"
    )
    credit = invoice_email("Slack", pdf, received=august(18), subject="Your Slack credit note")

    result = collection.run([credit])

    assert result.gaps == [Gap("Slack", "missing", ENGINEERING, None)]


def test_renewal_reminder_warns_of_an_upcoming_charge(collection: Collection) -> None:
    reminder = notice(
        "GitHub",
        "Your GitHub plan renews on 1 September",
        "Your annual plan will renew for $2,520.00.",
        received=august(25),
    )

    result = collection.run([reminder])

    [upcoming] = result.upcoming
    assert upcoming.vendor == "GitHub"
    assert upcoming.note == "Your GitHub plan renews on 1 September"


def test_credit_note_alone_does_not_stand_in_for_an_invoice(collection: Collection) -> None:
    collection.expect("Slack")
    pdf = b"%PDF-1.7 slack credit"
    collection.answers[pdf] = usd("Slack", date(2026, 8, 18), "45.00", "credit_note")
    credit = invoice_email("Slack", pdf, received=august(18), subject="Your Slack credit note")

    result = collection.run([credit])

    assert [gap.vendor for gap in result.gaps] == ["Slack"]


# Source accounts that cannot be read


def test_source_account_that_cannot_be_read_does_not_stop_the_others(
    collection: Collection,
) -> None:
    collection.unavailable[OPS] = "the sign-in for ops@nyayalabs.example no longer works"

    result = collection.run(slack_invoice(collection))

    assert [row.vendor for row in result.summary] == ["Slack"]
    assert result.failed_source_accounts == {
        OPS: "the sign-in for ops@nyayalabs.example no longer works"
    }


def test_gap_is_unknown_when_the_vendors_source_account_could_not_be_read(
    collection: Collection,
) -> None:
    collection.expect("Zoom", OPS)
    collection.expect("Figma", FINANCE)
    collection.source_accounts = (FINANCE,)
    collection.unavailable[OPS] = "sign-in expired"

    result = collection.run([])

    assert result.gaps == [
        Gap("Figma", "missing", FINANCE, None),
        Gap("Zoom", "unknown", OPS, f"{OPS} could not be read"),
    ]


def test_vendor_with_no_source_account_is_unknown_if_any_account_could_not_be_read(
    collection: Collection,
) -> None:
    collection.expect("Canva", None)
    collection.unavailable[OPS] = "sign-in expired"

    result = collection.run([])

    assert result.gaps == [Gap("Canva", "unknown", None, f"{OPS} could not be read")]


def test_source_account_that_recovers_is_no_longer_reported(collection: Collection) -> None:
    collection.expect("Zoom", OPS)
    collection.unavailable[OPS] = "sign-in expired"
    collection.run([])
    collection.unavailable.clear()
    collection.source_accounts = (OPS,)

    result = collection.run([])

    assert result.failed_source_accounts == {}
    assert result.gaps == [Gap("Zoom", "missing", OPS, None)]


# Expected and suggested vendors


def test_vendor_that_billed_and_is_on_no_list_is_suggested(collection: Collection) -> None:
    collection.expect("Zoom", OPS)

    result = collection.run(slack_invoice(collection))

    [suggested] = result.suggested_vendors
    assert (suggested.vendor, suggested.status) == ("Slack", "suggested")
    assert (suggested.source_account, suggested.billing_cycle) == (ENGINEERING, "monthly")
    assert str(suggested.usual_amount) == "652.50"


def test_suggested_vendor_is_not_expected_until_accepted(collection: Collection) -> None:
    collection.run(slack_invoice(collection), JULY)

    result = collection.run([])

    assert result.gaps == []


def test_vendor_seen_only_in_an_earlier_month_is_suggested(collection: Collection) -> None:
    pdf = b"%PDF-1.7 loom july"
    collection.answers[pdf] = usd("Loom", date(2026, 7, 9), "150.00")
    july = [invoice_email("Loom", pdf, received=datetime(2026, 7, 9, 9, 0, tzinfo=UTC))]
    collection.run(july, JULY)

    result = collection.run([])

    assert [v.vendor for v in result.suggested_vendors] == ["Loom"]


def test_ignored_vendor_is_not_suggested_again(collection: Collection) -> None:
    collection.ledger.save_expected_vendor(
        ExpectedVendor("Slack", ENGINEERING, "monthly", None, None, None, status="ignored")
    )

    result = collection.run(slack_invoice(collection))

    assert result.suggested_vendors == []
    assert result.gaps == []


def test_expected_vendor_list_is_seeded_on_first_run_only(collection: Collection) -> None:
    first = [ExpectedVendor("Slack", ENGINEERING, "monthly", None, None, "USD")]
    later = [ExpectedVendor("Zoom", OPS, "monthly", None, None, "USD")]

    seed_expected_vendors(collection.ledger, first)
    seed_expected_vendors(collection.ledger, later)

    assert [v.vendor for v in collection.ledger.expected_vendors()] == ["Slack"]


def test_gaps_are_for_the_month_that_was_run(collection: Collection) -> None:
    collection.expect("Slack")
    collection.run(slack_invoice(collection), AUGUST)

    july = collection.run([], JULY)

    assert [gap.vendor for gap in july.gaps] == ["Slack"]
