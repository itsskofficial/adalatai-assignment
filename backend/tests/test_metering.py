"""Metering a run's model calls: what each adapter reports, and what the calls cost."""

import json
import threading
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from conftest import ReplayClient

from invoice_collector.claude_classifier import ClaudeClassifier
from invoice_collector.claude_extractor import ClaudeExtractor
from invoice_collector.claude_vendor_matcher import ClaudeVendorMatcher
from invoice_collector.domain import Email, ModelUsage
from invoice_collector.evals.metering import cost_usd as eval_cost_usd
from invoice_collector.extractor import ExtractionFailed
from invoice_collector.metering import (
    PRICES_PER_MILLION_TOKENS,
    Call,
    FakeMeter,
    RunMeter,
    cost_usd,
    describe_cost,
)

RECORDED = Path(__file__).parent / "recorded"
# 2,365 input and 57 output tokens.
SLACK_INVOICE: dict[str, Any] = json.loads(
    (RECORDED / "claude_slack_invoice.json").read_text("utf-8")
)
# 491 input and 24 output tokens.
PAYMENT_FAILED: dict[str, Any] = json.loads(
    (RECORDED / "claude_payment_failed.json").read_text("utf-8")
)
NOTICE = Email(
    source_account="ops@nyayalabs.example",
    message_id="m-failed-1",
    sender="Notion <team@mail.notion.so>",
    subject="Your payment failed",
    received_at=datetime(2026, 8, 12, 4, 0, tzinfo=UTC),
    text_body="We could not process your payment of $190.00 for Notion Plus.",
)


# What each Claude adapter reports


def test_claude_extractor_reports_the_tokens_of_its_call_and_the_model_it_asked(
    replay_client: ReplayClient,
) -> None:
    meter = FakeMeter()
    extractor = ClaudeExtractor(replay_client(200, SLACK_INVOICE), "claude-sonnet-5-5", meter)

    extractor.extract(b"%PDF-1.7 any document")

    # The model asked for, not the dated one the response names, which no price is kept for.
    assert meter.calls == [Call("claude-sonnet-5-5", 2365, 57)]


def test_claude_call_whose_answer_is_refused_is_still_reported(
    replay_client: ReplayClient,
) -> None:
    meter = FakeMeter()
    refusal: dict[str, Any] = {**SLACK_INVOICE, "stop_reason": "refusal", "content": []}
    extractor = ClaudeExtractor(replay_client(200, refusal), meter=meter)

    with pytest.raises(ExtractionFailed):
        extractor.extract(b"%PDF-1.7 any document")

    assert meter.calls == [Call("claude-haiku-4-5", 2365, 57)]


def test_claude_classifier_reports_the_tokens_of_its_call(replay_client: ReplayClient) -> None:
    meter = FakeMeter()

    ClaudeClassifier(replay_client(200, PAYMENT_FAILED), meter=meter).classify(NOTICE)

    assert meter.calls == [Call("claude-haiku-4-5", 491, 24)]


def test_claude_vendor_matcher_reports_the_tokens_of_its_call(
    replay_client: ReplayClient,
) -> None:
    meter = FakeMeter()
    text = json.dumps({"vendor": "Notion", "confidence": "high"})
    answer = {**PAYMENT_FAILED, "content": [{**PAYMENT_FAILED["content"][0], "text": text}]}

    ClaudeVendorMatcher(replay_client(200, answer), meter=meter).match("Notion", ["Notion"])

    assert meter.calls == [Call("claude-haiku-4-5", 491, 24)]


def test_claude_call_that_failed_reports_nothing(replay_client: ReplayClient) -> None:
    meter = FakeMeter()
    error = {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}

    with pytest.raises(ExtractionFailed):
        ClaudeExtractor(replay_client(529, error), meter=meter).extract(b"%PDF-1.7")

    assert meter.calls == []


# What the calls of a run cost


def test_cost_is_priced_per_million_tokens_of_each_kind() -> None:
    assert cost_usd("claude-haiku-4-5", 2_000_000, 100_000) == Decimal("2.50")
    assert cost_usd("claude-sonnet-5-5", 1_000_000, 1_000_000) == Decimal("12.00")
    assert cost_usd("jev-latest", 1_000_000, 1_000_000) == Decimal("0.042")


def test_model_with_no_known_price_has_an_unknown_cost() -> None:
    assert cost_usd("claude-unknown-9", 1000, 10) is None


def test_run_meter_counts_calls_and_tokens_per_model() -> None:
    meter = RunMeter()

    meter.record("jev-latest", 212, 8)
    meter.record("claude-haiku-4-5", 2365, 57)
    meter.record("claude-haiku-4-5", 491, 24)

    assert meter.usage() == (
        ModelUsage("claude-haiku-4-5", 2, 2856, 81, Decimal("0.003261")),
        ModelUsage("jev-latest", 1, 212, 8, Decimal("0.000008904")),
    )


def test_run_meter_with_no_calls_has_no_usage() -> None:
    assert RunMeter().usage() == ()


def test_model_with_no_known_price_leaves_its_cost_unknown_and_counts_its_tokens() -> None:
    meter = RunMeter()

    meter.record("claude-unknown-9", 1000, 10)

    assert meter.usage() == (ModelUsage("claude-unknown-9", 1, 1000, 10, None),)


def test_call_that_did_not_report_its_tokens_leaves_the_cost_unknown() -> None:
    meter = RunMeter()

    meter.record("jev-latest", 212, 8)
    meter.record("jev-latest", None, None)

    assert meter.usage() == (ModelUsage("jev-latest", 2, 212, 8, None),)


def test_run_meter_counts_every_call_made_on_several_threads_at_once() -> None:
    meter = RunMeter()
    start = threading.Barrier(8)

    def call_many() -> None:
        start.wait()
        for _ in range(1000):
            meter.record("claude-haiku-4-5", 3, 1)

    threads = [threading.Thread(target=call_many) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    [usage] = meter.usage()
    assert (usage.calls, usage.input_tokens, usage.output_tokens) == (8000, 24000, 8000)


# What a cost says


@pytest.mark.parametrize(
    ("models", "said"),
    [
        (None, "not recorded"),
        ((), "$0 (no model was called)"),
        ((ModelUsage("claude-haiku-4-5", 2, 2856, 81, Decimal("0.003261")),), "$0.0033"),
        ((ModelUsage("jev-latest", 1, 212, 8, Decimal("0.000008904")),), "under $0.0001"),
        (
            (
                ModelUsage("claude-haiku-4-5", 1, 491, 24, Decimal("0.000611")),
                ModelUsage("claude-unknown-9", 1, 1000, 10, None),
            ),
            "unknown: the cost of claude-unknown-9 is not known",
        ),
    ],
)
def test_cost_is_described_in_words(models: tuple[ModelUsage, ...] | None, said: str) -> None:
    assert describe_cost(models) == said


# The evals price calls from the same table


def test_evals_price_calls_from_the_run_s_table() -> None:
    for model in PRICES_PER_MILLION_TOKENS:
        provider = "jev" if model.startswith("jev") else "anthropic"
        expected = cost_usd(model, 4600, 60)
        assert expected is not None
        assert eval_cost_usd(provider, model, 4600, 60) == pytest.approx(float(expected))
    assert eval_cost_usd("none", "rules", 4600, 60) == 0.0
