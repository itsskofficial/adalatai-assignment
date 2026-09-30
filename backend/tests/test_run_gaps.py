"""Tests at the run seam: expected vendors, gaps, and source accounts that cannot be read."""

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, date, datetime

from support import (
    AUGUST,
    ENGINEERING,
    FINANCE,
    JULY,
    OPS,
    Collection,
    invoice_email,
    notice,
    portal_email,
    real_pdf,
    usd,
)

from invoice_collector.classifier import FakeClassifier
from invoice_collector.domain import (
    Classification,
    Email,
    EmailState,
    ExpectedVendor,
    Gap,
    InvoiceFormat,
)
from invoice_collector.portal import LoginGated
from invoice_collector.run import seed_expected_vendors
from invoice_collector.vendor_matcher import RulesFirstVendorMatcher, VendorMatch


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

    assert result.gaps == [Gap("Zoom", "missing", OPS, None, "not_received")]


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

    assert result.gaps == [
        Gap("Zoom", "missing", OPS, "payment failed on 12 August", "payment_failed")
    ]


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
            "held_for_review",
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

    assert result.gaps == [Gap("Slack", "missing", ENGINEERING, None, "not_received")]


# An email of the vendor that the tool holds, or could not process


WORKSPACE_PORTAL = "https://admin.google.example/billing/invoices"
BEHIND_A_SIGN_IN = (
    "its invoice is behind a portal that needs a sign-in: "
    "download it and upload it on the Review screen"
)


class Model:
    """Stands in for a model asked what rules cannot decide."""

    def __init__(self, answers: dict[str, str]) -> None:
        self.answers = answers

    def match(self, text: str, expected_vendors: Sequence[str]) -> VendorMatch:
        for named, vendor in self.answers.items():
            if text.startswith(f"{named}\n") and vendor in expected_vendors:
                return VendorMatch(vendor, 0.97, "a model")
        return VendorMatch(None, 0.9, "a model")


def workspace_invoice(collection: Collection, **options: str) -> Email:
    collection.pages[WORKSPACE_PORTAL] = LoginGated()
    return portal_email("Google Workspace", WORKSPACE_PORTAL, received=august(2), **options)


def test_gap_is_explained_by_an_invoice_behind_a_portal_that_needs_a_sign_in(
    collection: Collection,
) -> None:
    collection.expect("Google Workspace")

    result = collection.run([workspace_invoice(collection)])

    assert result.gaps == [
        Gap("Google Workspace", "missing", ENGINEERING, BEHIND_A_SIGN_IN, "manual_download")
    ]


def test_email_behind_a_sign_in_naming_the_vendor_another_way_explains_its_gap(
    collection: Collection,
) -> None:
    collection.expect("Google Workspace")
    collection.vendor_matcher = RulesFirstVendorMatcher(Model({"Google": "Google Workspace"}))
    email = workspace_invoice(
        collection,
        sender="Google <payments-noreply@google.example>",
        subject="Your invoice is available",
    )

    result = collection.run([email])

    [gap] = result.gaps
    assert (gap.vendor, gap.explanation) == ("Google Workspace", BEHIND_A_SIGN_IN)


def test_email_behind_a_sign_in_still_explains_the_gap_when_the_month_is_run_again(
    collection: Collection,
) -> None:
    collection.expect("Google Workspace")
    emails = [workspace_invoice(collection)]
    collection.run(emails)

    result = collection.run(emails)

    [gap] = result.gaps
    assert gap.explanation == BEHIND_A_SIGN_IN


def test_email_held_behind_a_sign_in_by_an_earlier_version_explains_the_gap_on_a_rerun(
    collection: Collection,
) -> None:
    collection.expect("Google Workspace")
    email = workspace_invoice(collection)
    # As a version that did not record whose the email is left it.
    collection.ledger.record(
        AUGUST,
        email,
        EmailState.NEEDS_REVIEW,
        reason="manual download needed",
        invoice_format=InvoiceFormat.PORTAL_LINK,
        portal_link=WORKSPACE_PORTAL,
    )

    result = collection.run([email])

    [gap] = result.gaps
    assert gap.explanation == BEHIND_A_SIGN_IN


def test_credit_note_behind_a_sign_in_does_not_explain_a_gap(collection: Collection) -> None:
    collection.expect("Google Workspace")
    credit = workspace_invoice(collection, subject="Your Google Workspace credit note")

    result = collection.run([credit])

    assert result.gaps == [Gap("Google Workspace", "missing", ENGINEERING, None, "not_received")]


def test_gap_is_explained_by_an_email_of_the_vendor_that_failed(collection: Collection) -> None:
    collection.expect("Slack")
    unreadable = invoice_email("Slack", real_pdf("slack august"), received=august(3))

    result = collection.run([unreadable])

    [gap] = result.gaps
    assert gap.explanation == "an email from it failed: no prepared answer for this document"


def test_explanation_names_the_mailbox_when_the_email_came_to_another_than_expected(
    collection: Collection,
) -> None:
    collection.expect("Slack", OPS)
    unreadable = invoice_email("Slack", real_pdf("slack august"), received=august(3))

    result = collection.run([unreadable])

    [gap] = result.gaps
    assert gap.source_account == OPS
    assert gap.explanation == (
        f"an email from it in {ENGINEERING} failed: no prepared answer for this document"
    )


def test_invoice_behind_a_sign_in_in_another_mailbox_is_named_with_it(
    collection: Collection,
) -> None:
    collection.expect("Google Workspace", OPS)

    result = collection.run([workspace_invoice(collection)])

    [gap] = result.gaps
    assert gap.explanation == f"{BEHIND_A_SIGN_IN} in {ENGINEERING}"


def test_gap_is_explained_by_an_email_of_the_vendor_that_could_not_be_classified(
    collection: Collection,
) -> None:
    collection.expect("Slack")
    email = invoice_email("Slack", b"%PDF-1.7 slack august", received=august(3))
    collection.classifier = FakeClassifier(failing=frozenset({email.message_id}))

    result = collection.run([email])

    [gap] = result.gaps
    assert gap.explanation == "an email from it failed: the classifier is unavailable"


class Unreachable(FakeClassifier):
    def classify(self, email: Email) -> Classification:
        raise ConnectionResetError("the connection was reset")


def test_gap_is_explained_by_an_email_of_the_vendor_whose_retries_were_spent(
    collection: Collection,
) -> None:
    collection.expect("Slack")
    collection.classifier = Unreachable()

    result = collection.run([invoice_email("Slack", b"%PDF-1.7 slack", received=august(3))])

    [gap] = result.gaps
    assert gap.explanation == (
        "an email from it failed: could not be examined: ConnectionResetError: "
        "the connection was reset"
    )


def test_gap_is_explained_by_a_pdf_that_could_not_be_opened(collection: Collection) -> None:
    collection.expect("Slack")
    damaged = b"%PDF-1.7\n1 0 obj << /Type /Catalog"

    result = collection.run([invoice_email("Slack", damaged, received=august(3))])

    [gap] = result.gaps
    assert gap.explanation == (
        "held for review: the PDF is damaged, so nothing could be read from it, and 4 more"
    )


def test_email_of_another_vendor_that_failed_does_not_explain_a_gap(
    collection: Collection,
) -> None:
    collection.expect("Zoom", OPS)
    unreadable = invoice_email("Slack", real_pdf("slack august"), received=august(3))

    result = collection.run([unreadable])

    assert result.gaps == [Gap("Zoom", "missing", OPS, None, "not_received")]


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
        Gap("Figma", "missing", FINANCE, None, "not_received"),
        Gap("Zoom", "unknown", OPS, f"{OPS} could not be read", "mailbox_unread"),
    ]


def test_vendor_with_no_source_account_is_unknown_if_any_account_could_not_be_read(
    collection: Collection,
) -> None:
    collection.expect("Canva", None)
    collection.unavailable[OPS] = "sign-in expired"

    result = collection.run([])

    assert result.gaps == [
        Gap("Canva", "unknown", None, f"{OPS} could not be read", "mailbox_unread")
    ]


def test_source_account_that_recovers_is_no_longer_reported(collection: Collection) -> None:
    collection.expect("Zoom", OPS)
    collection.unavailable[OPS] = "sign-in expired"
    collection.run([])
    collection.unavailable.clear()
    collection.source_accounts = (OPS,)

    result = collection.run([])

    assert result.failed_source_accounts == {}
    assert result.gaps == [Gap("Zoom", "missing", OPS, None, "not_received")]


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
