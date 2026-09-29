"""The extractors that touch the outside world, checked without calling the model.

The Claude extractor is pointed at a local server that replays recorded responses.
"""

import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from conftest import ReplayClient

from invoice_collector.claude_extractor import ClaudeExtractor
from invoice_collector.domain import Extraction
from invoice_collector.extractor import ExtractionFailed, NotABillingDocument
from invoice_collector.rule_extractor import RuleExtractor
from invoice_collector.samples import load_sources

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
    start, end = datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 9, 1, tzinfo=UTC)
    [pdf] = [
        attachment.content
        for source in load_sources(BACKEND / "samples")
        for email in source.emails_between(start, end)
        for attachment in email.attachments
    ]
    return pdf


def test_rules_read_a_clearly_laid_out_invoice() -> None:
    extraction = RuleExtractor(("Slack", "Notion")).extract(slack_sample_pdf())

    assert (extraction.vendor, extraction.invoice_date) == ("Slack", date(2026, 8, 3))
    assert (extraction.total, extraction.currency) == (Decimal("652.50"), "USD")
    assert extraction.document_type == "invoice"
    assert extraction.confidence == "low"


def test_rules_fail_when_the_vendor_is_not_known() -> None:
    with pytest.raises(ExtractionFailed, match="vendor"):
        RuleExtractor(("Notion",)).extract(slack_sample_pdf())


def test_rules_fail_on_a_file_that_is_not_a_pdf() -> None:
    with pytest.raises(ExtractionFailed, match="could not read the PDF"):
        RuleExtractor(("Slack",)).extract(b"not a pdf at all")


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
