"""The extractors that touch the outside world, checked without calling the model.

The Claude extractor is pointed at a local server that replays recorded responses.
"""

import json
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from conftest import ReplayClient

from invoice_collector.claude_extractor import ClaudeExtractor
from invoice_collector.domain import ExpectedVendor, Extraction
from invoice_collector.extractor import ExtractionFailed, Hints, NotABillingDocument, hints_for
from invoice_collector.rule_extractor import READ_BY_RULES, RuleExtractor

BACKEND = Path(__file__).parents[1]
RECORDED = json.loads((BACKEND / "tests/recorded/claude_slack_invoice.json").read_text("utf-8"))
PDF = b"%PDF-1.7 any document"

Replay = Callable[[int, dict[str, Any]], ClaudeExtractor]


def answering(fields: dict[str, str]) -> dict[str, Any]:
    """The recorded response, with the fields the model returned replaced."""
    recorded_fields = json.loads(RECORDED["content"][0]["text"])
    text = json.dumps({**recorded_fields, **fields})
    return {**RECORDED, "content": [{**RECORDED["content"][0], "text": text}]}


@pytest.fixture
def replay(replay_client: ReplayClient) -> Replay:
    return lambda status, body: ClaudeExtractor(replay_client(status, body))


def test_claude_reads_the_fields_of_an_invoice(replay: Replay) -> None:
    extraction = replay(200, RECORDED).extract(PDF)

    assert extraction == Extraction(
        document_type="invoice",
        vendor="Slack",
        invoice_date=date(2026, 8, 3),
        total=Decimal("652.50"),
        currency="USD",
        confidence="high",
        doubts="",
        by="claude-haiku-4-5",
    )


def test_claude_reports_a_document_that_is_not_a_billing_document(replay: Replay) -> None:
    response = answering({"document_type": "not_a_billing_document", "doubts": "a newsletter"})

    with pytest.raises(NotABillingDocument, match="a newsletter"):
        replay(200, response).extract(PDF)


def test_total_with_thousands_separators_is_read(replay: Replay) -> None:
    extraction = replay(200, answering({"total": "1,250.00"})).extract(PDF)

    assert extraction.total == Decimal("1250.00")


def test_unusable_date_from_the_model_fails(replay: Replay) -> None:
    with pytest.raises(ExtractionFailed, match="unusable value"):
        replay(200, answering({"invoice_date": "3rd of August"})).extract(PDF)


def test_error_from_the_model_fails(replay: Replay) -> None:
    error = {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}

    with pytest.raises(ExtractionFailed, match="HTTP 529"):
        replay(529, error).extract(PDF)


def test_refusal_from_the_model_fails(replay: Replay) -> None:
    refusal: dict[str, Any] = {**RECORDED, "stop_reason": "refusal", "content": []}

    with pytest.raises(ExtractionFailed, match="stopped early: refusal"):
        replay(200, refusal).extract(PDF)


def slack_sample_pdf() -> bytes:
    return (BACKEND / "tests/recorded/slack_invoice.pdf").read_bytes()


def test_rules_read_a_clearly_laid_out_invoice() -> None:
    hints = Hints(expected_vendors=("Slack", "Notion"))

    extraction = RuleExtractor().extract(slack_sample_pdf(), hints)

    assert (extraction.vendor, extraction.invoice_date) == ("Slack", date(2026, 8, 3))
    assert (extraction.total, extraction.currency) == (Decimal("652.50"), "USD")
    assert extraction.document_type == "invoice"
    assert extraction.confidence == "low"
    assert extraction.doubts == READ_BY_RULES


def test_rules_know_no_vendor_of_their_own() -> None:
    with pytest.raises(ExtractionFailed, match="could not identify the vendor"):
        RuleExtractor().extract(slack_sample_pdf())


def test_rules_fail_when_the_vendor_is_not_known() -> None:
    with pytest.raises(ExtractionFailed, match="vendor"):
        RuleExtractor().extract(slack_sample_pdf(), Hints(expected_vendors=("Notion",)))


def test_rules_take_the_vendor_the_email_names_when_none_expected_is_in_the_document() -> None:
    hints = Hints(expected_vendors=("Notion",), named_by_email=("Slack",))

    assert RuleExtractor().extract(slack_sample_pdf(), hints).vendor == "Slack"


def test_rules_prefer_an_expected_vendor_to_the_name_the_email_gives() -> None:
    hints = Hints(expected_vendors=("slack",), named_by_email=("Slack Technologies",))

    assert RuleExtractor().extract(slack_sample_pdf(), hints).vendor == "slack"


def test_rules_do_not_guess_between_two_expected_vendors_in_one_document() -> None:
    hints = Hints(expected_vendors=("Slack", "Invoice"))

    with pytest.raises(ExtractionFailed, match="names Slack and Invoice"):
        RuleExtractor().extract(slack_sample_pdf(), hints)


def test_hints_hold_each_name_once_and_leave_out_the_empty() -> None:
    listed = [
        ExpectedVendor("Slack", None, "monthly", None, None, None),
        ExpectedVendor("Slack", None, "monthly", None, None, None),
    ]

    assert hints_for(listed, "Slack", None, " ") == Hints(("Slack",), ("Slack",))


def test_rules_fail_on_a_file_that_is_not_a_pdf() -> None:
    with pytest.raises(ExtractionFailed, match="could not read the PDF"):
        RuleExtractor().extract(b"not a pdf at all", Hints(expected_vendors=("Slack",)))


def test_answer_that_does_not_fit_the_fields_fails(replay: Replay) -> None:
    with pytest.raises(ExtractionFailed, match="did not fit"):
        replay(200, answering({"document_type": "bill"})).extract(PDF)


def test_answer_that_is_not_json_fails(replay: Replay) -> None:
    not_json = {**RECORDED, "content": [{**RECORDED["content"][0], "text": "Here you go!"}]}

    with pytest.raises(ExtractionFailed, match="did not fit"):
        replay(200, not_json).extract(PDF)


@pytest.mark.parametrize("vendor", ["", "   "])
def test_answer_with_no_vendor_fails(replay: Replay, vendor: str) -> None:
    with pytest.raises(ExtractionFailed, match="no vendor"):
        replay(200, answering({"vendor": vendor})).extract(PDF)


@pytest.mark.parametrize("currency", ["", "$", "dollars"])
def test_answer_with_no_currency_code_fails(replay: Replay, currency: str) -> None:
    with pytest.raises(ExtractionFailed, match="currency"):
        replay(200, answering({"currency": currency})).extract(PDF)


def test_currency_written_with_letters_outside_the_alphabet_fails(replay: Replay) -> None:
    with pytest.raises(ExtractionFailed, match="currency"):
        replay(200, answering({"currency": "éUR"})).extract(PDF)


@pytest.mark.parametrize("total", ["NaN", "Infinity", "-Infinity"])
def test_total_that_is_not_a_number_fails(replay: Replay, total: str) -> None:
    with pytest.raises(ExtractionFailed, match="unusable value"):
        replay(200, answering({"total": total})).extract(PDF)


def test_subtotal_and_tax_are_read_where_the_document_states_them(replay: Replay) -> None:
    extraction = replay(200, answering({"subtotal": "600.00", "tax": "52.50"})).extract(PDF)

    assert (extraction.subtotal, extraction.tax) == (Decimal("600.00"), Decimal("52.50"))


def test_subtotal_and_tax_are_absent_where_the_document_does_not_state_them(
    replay: Replay,
) -> None:
    extraction = replay(200, answering({"subtotal": "", "tax": ""})).extract(PDF)

    assert (extraction.subtotal, extraction.tax) == (None, None)
