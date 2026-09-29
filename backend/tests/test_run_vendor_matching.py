"""Tests at the run seam: the vendor a document names, matched to the expected vendor list.

The expected vendor list is the company's own spelling. A billing document that names the
vendor another way is filed and reported under the list's spelling, and what the document
said is kept in the ledger. See ADR 0009 and ADR 0016.
"""

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, date, datetime

from support import AUGUST, ENGINEERING, OPS, Collection, invoice_email, notice, usd

from invoice_collector.domain import Email, EmailState, Gap
from invoice_collector.vendor_matcher import (
    RulesFirstVendorMatcher,
    VendorMatch,
    VendorMatchFailed,
)

AWS_PDF = b"%PDF-1.7 amazon web services august"
AMAZON = usd("Amazon Web Services", date(2026, 8, 2), "2691.60")


def august(day: int) -> datetime:
    return datetime(2026, 8, day, 9, 0, tzinfo=UTC)


class Model:
    """Stands in for a model asked what rules cannot decide, remembering what it was asked."""

    def __init__(self, answers: dict[str, str] | None = None, *, fails: bool = False) -> None:
        self.answers = answers or {}
        self.fails = fails
        self.asked: list[str] = []

    def match(self, text: str, expected_vendors: Sequence[str]) -> VendorMatch:
        self.asked.append(text)
        if self.fails:
            raise VendorMatchFailed("Jev returned HTTP 503")
        for named, vendor in self.answers.items():
            if named in text and vendor in expected_vendors:
                return VendorMatch(vendor, 0.97)
        return VendorMatch(None, 0.9)


def aws_invoice(collection: Collection, subject: str = "Your invoice is available") -> list[Email]:
    collection.answers[AWS_PDF] = AMAZON
    return [invoice_email("Amazon", AWS_PDF, received=august(2), subject=subject)]


def knows_amazon() -> RulesFirstVendorMatcher:
    return RulesFirstVendorMatcher(Model({"Amazon Web Services": "AWS"}))


def test_document_naming_the_vendor_another_way_is_filed_under_the_expected_spelling(
    collection: Collection,
) -> None:
    collection.expect("AWS")
    collection.vendor_matcher = knows_amazon()

    result = collection.run(aws_invoice(collection))

    [row] = result.summary
    assert row.vendor == "AWS"
    assert collection.saved_files() == ["2026-08_AWS_2691.60-USD.pdf"]
    assert result.gaps == []
    assert result.suggested_vendors == []


def test_what_the_document_said_is_kept_in_the_ledger(collection: Collection) -> None:
    collection.expect("AWS")
    collection.vendor_matcher = knows_amazon()

    collection.run(aws_invoice(collection))

    [document] = collection.ledger.documents(AUGUST)
    assert document.extraction.vendor == "AWS"
    assert document.vendor_as_read == "Amazon Web Services"


def test_document_naming_the_vendor_as_listed_keeps_no_second_name(
    collection: Collection,
) -> None:
    collection.expect("AWS")
    collection.answers[AWS_PDF] = replace(AMAZON, vendor="AWS")

    collection.run([invoice_email("AWS", AWS_PDF, received=august(2))])

    [document] = collection.ledger.documents(AUGUST)
    assert (document.extraction.vendor, document.vendor_as_read) == ("AWS", None)


def test_legal_name_of_an_expected_vendor_takes_the_listed_spelling_without_a_model(
    collection: Collection,
) -> None:
    collection.expect("Slack")
    model = Model()
    collection.vendor_matcher = RulesFirstVendorMatcher(model)
    pdf = b"%PDF-1.7 slack august"
    collection.answers[pdf] = usd("Slack Technologies, LLC", date(2026, 8, 3), "652.50")

    result = collection.run([invoice_email("Slack", pdf, received=august(3))])

    assert [row.vendor for row in result.summary] == ["Slack"]
    assert model.asked == []


def test_rules_match_when_the_email_names_one_expected_vendor(collection: Collection) -> None:
    collection.expect("AWS")
    collection.expect("Slack")

    result = collection.run(aws_invoice(collection, subject="Your AWS invoice for August"))

    assert [row.vendor for row in result.summary] == ["AWS"]


def test_model_is_asked_only_what_rules_cannot_decide(collection: Collection) -> None:
    collection.expect("AWS")
    model = Model({"Amazon Web Services": "AWS"})
    collection.vendor_matcher = RulesFirstVendorMatcher(model)

    collection.run(aws_invoice(collection, subject="Your AWS invoice for August"))

    assert model.asked == []


def test_vendor_matching_no_expected_vendor_stands_and_is_suggested(
    collection: Collection,
) -> None:
    collection.expect("GitHub")
    collection.vendor_matcher = RulesFirstVendorMatcher(Model())

    result = collection.run(aws_invoice(collection))

    assert [row.vendor for row in result.summary] == ["Amazon Web Services"]
    assert [v.vendor for v in result.suggested_vendors] == ["Amazon Web Services"]
    [document] = collection.ledger.documents(AUGUST)
    assert document.vendor_as_read is None


def test_model_that_fails_leaves_the_name_as_read_and_the_email_collected(
    collection: Collection,
) -> None:
    collection.expect("AWS")
    collection.vendor_matcher = RulesFirstVendorMatcher(Model(fails=True))

    result = collection.run(aws_invoice(collection))

    assert [row.vendor for row in result.summary] == ["Amazon Web Services"]
    [examined] = collection.ledger.examined_emails(AUGUST)
    assert examined.state is EmailState.COLLECTED
    assert result.warnings == [
        "Your invoice is available: Amazon Web Services could not be matched to an "
        "expected vendor (Jev returned HTTP 503), so it is kept as read"
    ]


def test_second_model_is_asked_when_the_first_fails(collection: Collection) -> None:
    collection.expect("AWS")
    collection.vendor_matcher = RulesFirstVendorMatcher(
        Model(fails=True), Model({"Amazon Web Services": "AWS"})
    )

    result = collection.run(aws_invoice(collection))

    assert [row.vendor for row in result.summary] == ["AWS"]
    assert result.warnings == []


def test_usual_amount_of_the_expected_vendor_applies_to_the_matched_document(
    collection: Collection,
) -> None:
    collection.expect("AWS", usual="1000.00")
    collection.vendor_matcher = knows_amazon()

    result = collection.run(aws_invoice(collection))

    [pending] = result.pending
    assert pending.extraction.vendor == "AWS"
    assert pending.vendor_as_read == "Amazon Web Services"
    assert [d.reason for d in pending.doubts] == [
        "the total is 169% above the usual 1000.00 USD for AWS"
    ]
    assert collection.saved_files() == ["pending"]


def test_payment_failed_notice_naming_the_vendor_another_way_explains_its_gap(
    collection: Collection,
) -> None:
    collection.expect("Zoom", OPS)
    failed = notice(
        "Zoom Video Communications",
        "Your payment failed",
        "We could not process your payment of $149.90.",
        received=august(23),
        account=OPS,
    )

    result = collection.run([failed])

    assert result.gaps == [Gap("Zoom", "missing", OPS, "payment failed on 23 August")]


# Documents collected under the name as read, before the vendor was matched


def test_rerun_files_a_document_collected_under_another_name_under_the_expected_spelling(
    collection: Collection,
) -> None:
    collection.expect("AWS")
    emails = aws_invoice(collection)
    first = collection.run(emails)
    assert [row.vendor for row in first.summary] == ["Amazon Web Services"]
    [earlier] = collection.ledger.documents(AUGUST)
    collection.vendor_matcher = knows_amazon()

    result = collection.run(emails)

    [row] = result.summary
    assert row.vendor == "AWS"
    # A run never moves a filed PDF: the document keeps its file, and no copy is made.
    assert row.file_link == earlier.file_link
    assert collection.saved_files() == ["2026-08_AmazonWebServices_2691.60-USD.pdf"]
    [document] = collection.ledger.documents(AUGUST)
    assert document.vendor_as_read == "Amazon Web Services"
    assert result.gaps == []


def test_suggestion_left_with_no_charge_behind_it_is_withdrawn(collection: Collection) -> None:
    collection.expect("AWS")
    emails = aws_invoice(collection)
    first = collection.run(emails)
    assert [v.vendor for v in first.suggested_vendors] == ["Amazon Web Services"]
    collection.vendor_matcher = knows_amazon()

    result = collection.run(emails)

    assert result.suggested_vendors == []
    assert [v.vendor for v in collection.ledger.expected_vendors()] == ["AWS"]


def test_rerun_after_matching_asks_no_model_again(collection: Collection) -> None:
    collection.expect("AWS")
    model = Model({"Amazon Web Services": "AWS"})
    collection.vendor_matcher = RulesFirstVendorMatcher(model)
    emails = aws_invoice(collection)
    collection.run(emails)
    model.asked.clear()

    result = collection.run(emails)

    assert [row.vendor for row in result.summary] == ["AWS"]
    assert model.asked == []


def test_document_from_a_vendor_on_no_list_is_not_matched_to_another(
    collection: Collection,
) -> None:
    collection.expect("AWS", ENGINEERING)
    collection.vendor_matcher = knows_amazon()
    pdf = b"%PDF-1.7 loom"
    collection.answers[pdf] = usd("Loom", date(2026, 8, 9), "150.00")

    result = collection.run([invoice_email("Loom", pdf, received=august(9))])

    assert [row.vendor for row in result.summary] == ["Loom"]
