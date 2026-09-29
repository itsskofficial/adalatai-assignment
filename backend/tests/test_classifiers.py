"""The Claude classifier, checked against recorded responses without calling the model."""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from conftest import ReplayClient

from invoice_collector.classifier import ClassificationFailed
from invoice_collector.claude_classifier import ClaudeClassifier
from invoice_collector.domain import Classification, Email

RECORDED = json.loads(
    (Path(__file__).parent / "recorded/claude_payment_failed.json").read_text("utf-8")
)
NOTICE = Email(
    source_account="ops@nyayalabs.example",
    message_id="m-failed-1",
    sender="Notion <team@mail.notion.so>",
    subject="Your payment failed",
    received_at=datetime(2026, 8, 12, 4, 0, tzinfo=UTC),
    text_body="We could not process your payment of $190.00 for Notion Plus.",
)

Replay = Callable[[int, dict[str, Any]], ClaudeClassifier]


def answering(fields: dict[str, str]) -> dict[str, Any]:
    """The recorded response, with the fields the model returned replaced."""
    recorded_fields = json.loads(RECORDED["content"][0]["text"])
    text = json.dumps({**recorded_fields, **fields})
    return {**RECORDED, "content": [{**RECORDED["content"][0], "text": text}]}


@pytest.fixture
def replay(replay_client: ReplayClient) -> Replay:
    return lambda status, body: ClaudeClassifier(replay_client(status, body))


def test_claude_classifies_a_payment_failed_notice(replay: Replay) -> None:
    classification = replay(200, RECORDED).classify(NOTICE)

    assert classification == Classification(
        "payment_failed", "Notion", "high", by="claude-haiku-4-5"
    )


def test_email_that_is_not_about_billing_has_no_vendor(replay: Replay) -> None:
    response = answering({"kind": "not_billing", "vendor": ""})

    assert replay(200, response).classify(NOTICE) == Classification(
        "not_billing", None, "high", by="claude-haiku-4-5"
    )


def test_error_from_the_model_fails(replay: Replay) -> None:
    error = {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}

    with pytest.raises(ClassificationFailed, match="HTTP 529"):
        replay(529, error).classify(NOTICE)


def test_refusal_from_the_model_fails(replay: Replay) -> None:
    refusal: dict[str, Any] = {**RECORDED, "stop_reason": "refusal", "content": []}

    with pytest.raises(ClassificationFailed, match="stopped early: refusal"):
        replay(200, refusal).classify(NOTICE)


def test_answer_that_does_not_fit_the_kinds_fails(replay: Replay) -> None:
    with pytest.raises(ClassificationFailed, match="did not fit"):
        replay(200, answering({"kind": "bill"})).classify(NOTICE)


def test_answer_that_is_not_json_fails(replay: Replay) -> None:
    not_json: dict[str, Any] = {
        **RECORDED,
        "content": [{**RECORDED["content"][0], "text": "This looks like a failed payment."}],
    }

    with pytest.raises(ClassificationFailed, match="did not fit"):
        replay(200, not_json).classify(NOTICE)
