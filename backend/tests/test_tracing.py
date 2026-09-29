"""Tracing of model calls, at the seam of each model's adapter. See ADR 0017.

The adapters are pointed at local servers replaying recorded responses, and trace to a
fake tracer. Nothing reaches Claude, Jev or Langfuse.
"""

import json
import re
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import ReplayClient
from test_jev import (
    KIND,
    NOTICE,
    VENDOR,
    StartJev,
    jev_server,  # noqa: F401  # pyright: ignore[reportUnusedImport]
)

from invoice_collector import tracing, trail
from invoice_collector.api.questions import Answerer, Ledgered
from invoice_collector.classifier import ClassificationFailed
from invoice_collector.claude_classifier import ClaudeClassifier
from invoice_collector.claude_extractor import ClaudeExtractor
from invoice_collector.claude_vendor_matcher import ClaudeVendorMatcher
from invoice_collector.extractor import ExtractionFailed
from invoice_collector.jev_classifier import JevClassifier
from invoice_collector.metering import Call, FakeMeter, RunMeter
from invoice_collector.tracing import (
    NO_TRACER,
    Content,
    FakeTracer,
    Finished,
    TraceContext,
    TraceLink,
)
from invoice_collector.vendor_matcher import JevVendorMatcher

RECORDED = Path(__file__).parent / "recorded"
INVOICE: dict[str, Any] = json.loads((RECORDED / "claude_slack_invoice.json").read_text("utf-8"))
CLASSIFIED: dict[str, Any] = json.loads(
    (RECORDED / "claude_payment_failed.json").read_text("utf-8")
)
TOOL_USE: dict[str, Any] = json.loads(
    (RECORDED / "claude_question_tool_use.json").read_text("utf-8")
)
PDF = (RECORDED / "slack_invoice.pdf").read_bytes()
SRC = Path(__file__).resolve().parents[1] / "src" / "invoice_collector"


def only_call(tracer: FakeTracer) -> tuple[tracing.FakeCall, Finished]:
    assert len(tracer.calls) == 1
    call = tracer.calls[0]
    assert call.finished is not None
    return call, call.finished


# --- Choosing the tracer ---------------------------------------------------------------------


def test_no_langfuse_keys_means_no_tracing() -> None:
    assert tracing.tracer_from_environment({}) is NO_TRACER
    assert tracing.tracer_from_environment({"LANGFUSE_PUBLIC_KEY": "pk-lf-1"}) is NO_TRACER
    assert tracing.tracer_from_environment({"LANGFUSE_SECRET_KEY": "sk-lf-1"}) is NO_TRACER
    blank = {"LANGFUSE_PUBLIC_KEY": " ", "LANGFUSE_SECRET_KEY": "sk-lf-1"}
    assert tracing.tracer_from_environment(blank) is NO_TRACER


def test_both_langfuse_keys_choose_langfuse_at_the_host_given() -> None:
    from invoice_collector.langfuse_tracer import LangfuseTracer

    tracer = tracing.tracer_from_environment(
        {
            "LANGFUSE_PUBLIC_KEY": "pk-lf-choosing",
            "LANGFUSE_SECRET_KEY": "sk-lf-choosing",
            "LANGFUSE_HOST": "http://127.0.0.1:9/",
        }
    )

    assert isinstance(tracer, LangfuseTracer)
    assert not tracer.sends_content
    call = tracer.start("extraction", "claude-haiku-4-5", TraceContext(), None)
    assert call.link is not None
    assert call.link.url == f"http://127.0.0.1:9/trace/{call.link.trace_id}"
    assert re.fullmatch(r"[0-9a-f]{32}", call.link.trace_id)
    tracer.close(1.0)


def test_sending_content_is_an_explicit_setting() -> None:
    tracer = tracing.tracer_from_environment(
        {
            "LANGFUSE_PUBLIC_KEY": "pk-lf-content",
            "LANGFUSE_SECRET_KEY": "sk-lf-content",
            "LANGFUSE_HOST": "http://127.0.0.1:9",
            "INVOICE_COLLECTOR_TRACE_CONTENT": "1",
        }
    )

    from invoice_collector.langfuse_tracer import LangfuseTracer

    assert isinstance(tracer, LangfuseTracer)
    assert tracer.sends_content
    tracer.close(1.0)


def test_only_the_langfuse_tracer_imports_langfuse() -> None:
    importing = [
        path.relative_to(SRC).as_posix()
        for path in SRC.rglob("*.py")
        if re.search(r"^\s*(from|import)\s+langfuse\b", path.read_text("utf-8"), re.M)
    ]

    assert importing == ["langfuse_tracer.py"]


# --- Each model call is traced -----------------------------------------------------------------


def test_an_extraction_is_traced_with_model_tokens_cost_time_and_fields(
    replay_client: ReplayClient,
) -> None:
    tracer = FakeTracer()
    extractor = ClaudeExtractor(replay_client(200, INVOICE), tracer=tracer)

    extraction = extractor.extract(PDF)

    call, finished = only_call(tracer)
    assert (call.step, call.model) == ("extraction", "claude-haiku-4-5")
    assert (finished.input_tokens, finished.output_tokens) == (2365, 57)
    assert finished.cost_usd == pytest.approx((2365 * 1.00 + 57 * 5.00) / 1_000_000)
    assert finished.seconds > 0
    assert finished.output is not None
    assert finished.output["vendor"] == extraction.vendor
    assert finished.output["total"] == "652.50"
    # The model's free-text note is not sent, and neither is the PDF.
    assert "doubts" not in finished.output
    assert call.content is None


def test_the_meter_and_the_trace_count_the_same_tokens_of_one_call(
    replay_client: ReplayClient,
) -> None:
    tracer, meter = FakeTracer(), RunMeter()
    extractor = ClaudeExtractor(replay_client(200, INVOICE), meter=meter, tracer=tracer)

    extractor.extract(PDF)

    _, finished = only_call(tracer)
    (usage,) = meter.usage()
    assert (usage.model, usage.calls) == ("claude-haiku-4-5", 1)
    assert (usage.input_tokens, usage.output_tokens) == (
        finished.input_tokens,
        finished.output_tokens,
    )
    # Both are priced from the one table.
    assert usage.cost_usd is not None and finished.cost_usd == float(usage.cost_usd)


def test_a_failed_call_is_traced_but_not_metered(replay_client: ReplayClient) -> None:
    tracer, meter = FakeTracer(), FakeMeter()
    extractor = ClaudeExtractor(replay_client(529, {"type": "error"}), meter=meter, tracer=tracer)

    with pytest.raises(ExtractionFailed):
        extractor.extract(PDF)

    assert len(tracer.calls) == 1
    assert meter.calls == []


def test_a_model_with_no_price_is_traced_with_no_cost() -> None:
    tracer, meter = FakeTracer(), FakeMeter()
    response = SimpleNamespace(usage=SimpleNamespace(input_tokens=10, output_tokens=2))
    call = tracing.traced(
        tracer,
        "extraction",
        "a-model-with-no-price",
        "anthropic",
        lambda: response,
        output=lambda _: {},
        meter=meter,
    )

    call()

    _, finished = only_call(tracer)
    assert (finished.input_tokens, finished.cost_usd) == (10, None)
    assert meter.calls == [Call("a-model-with-no-price", 10, 2)]


def test_the_full_input_is_sent_only_when_asked(replay_client: ReplayClient) -> None:
    tracer = FakeTracer(sends_content=True)

    ClaudeExtractor(replay_client(200, INVOICE), tracer=tracer).extract(PDF)

    call, _ = only_call(tracer)
    assert call.content is not None
    assert call.content.pdf == PDF
    assert call.content.text is not None and "billing fields" in call.content.text


def test_a_failed_call_is_traced_as_failed_and_still_fails(replay_client: ReplayClient) -> None:
    tracer = FakeTracer()
    extractor = ClaudeExtractor(replay_client(529, {"type": "error"}), tracer=tracer)

    with pytest.raises(ExtractionFailed, match="HTTP 529"):
        extractor.extract(PDF)

    _, finished = only_call(tracer)
    assert finished.error == "OverloadedError (HTTP 529)"
    assert finished.output is None


def test_a_classification_is_traced(replay_client: ReplayClient) -> None:
    tracer = FakeTracer()

    ClaudeClassifier(replay_client(200, CLASSIFIED), tracer=tracer).classify(NOTICE)

    call, finished = only_call(tracer)
    assert (call.step, call.model) == ("classification", "claude-haiku-4-5")
    assert (finished.input_tokens, finished.output_tokens) == (491, 24)
    assert finished.output == {"kind": "payment_failed", "vendor": "Notion", "confidence": "high"}


def test_a_vendor_match_by_claude_is_traced(replay_client: ReplayClient) -> None:
    tracer = FakeTracer()
    text = json.dumps({"vendor": "Slack", "confidence": "high"})
    answer = {**CLASSIFIED, "content": [{**CLASSIFIED["content"][0], "text": text}]}

    ClaudeVendorMatcher(replay_client(200, answer), tracer=tracer).match("Slack", ["Slack"])

    call, finished = only_call(tracer)
    assert call.step == "vendor matching"
    assert finished.output == {"vendor": "Slack", "confidence": "high"}


def test_calls_to_jev_are_traced_with_their_tokens(
    jev_server: StartJev,  # noqa: F811
) -> None:
    tracer = FakeTracer()
    classifier = JevClassifier(
        "not-a-real-key", base_url=jev_server(200, KIND).base_url, max_retries=0, tracer=tracer
    )
    matcher = JevVendorMatcher(
        "not-a-real-key", base_url=jev_server(200, VENDOR).base_url, max_retries=0, tracer=tracer
    )

    classifier.classify(NOTICE)
    matcher.match("An invoice", ["Slack", "Notion", "Figma"])

    assert [(c.step, c.model) for c in tracer.calls] == [
        ("classification", "jev-latest"),
        ("vendor matching", "jev-latest"),
    ]
    first = tracer.calls[0].finished
    assert first is not None
    assert (first.input_tokens, first.output_tokens) == (212, 8)
    assert first.cost_usd == pytest.approx(212 * 0.042 / 1_000_000)
    assert first.output is not None and first.output["choice"] == "payment_failed"


def test_a_question_is_traced_with_the_query_chosen_but_not_the_question(
    replay_client: ReplayClient, tmp_path: Path
) -> None:
    tracer = FakeTracer()
    answerer = Answerer(
        replay_client(200, TOOL_USE), tmp_path / "log", lambda: date(2026, 9, 29), tracer=tracer
    )

    answerer.choose("How much did AWS cost this summer?", Ledgered(charges=[], months=[]))

    call, finished = only_call(tracer)
    assert call.step == "question"
    assert call.content is None
    assert finished.output == {
        "chosen": [
            {
                "query": "total_spend",
                "parameters": {
                    "vendor": "AWS",
                    "source_account": None,
                    "from_month": "2026-06",
                    "to_month": "2026-08",
                },
            }
        ]
    }


def test_a_tracer_that_fails_changes_nothing_the_model_answered(
    replay_client: ReplayClient,
) -> None:
    traced = ClaudeClassifier(replay_client(200, CLASSIFIED), tracer=FakeTracer(failing=True))
    plain = ClaudeClassifier(replay_client(200, CLASSIFIED))

    assert traced.classify(NOTICE) == plain.classify(NOTICE)


def test_a_tracer_that_fails_leaves_a_failed_call_failing(replay_client: ReplayClient) -> None:
    classifier = ClaudeClassifier(replay_client(500, {}), tracer=FakeTracer(failing=True))

    with pytest.raises(ClassificationFailed, match="HTTP 500"):
        classifier.classify(NOTICE)


# --- What a call is about ------------------------------------------------------------------


def test_a_scope_says_what_calls_are_about_and_keeps_their_traces() -> None:
    tracer = FakeTracer()
    call = tracing.traced(
        tracer, "extraction", "claude-haiku-4-5", "anthropic", lambda: object(), output=lambda _: {}
    )

    with tracing.scope(run_id=7, collection_month="2026-08", message_id="m-1") as outer:
        with tracing.scope(step="escalated extraction", document="abc") as inner:
            call()
        assert inner.latest == tracer.calls[0].link
        call()

    assert tracer.calls[0].context == TraceContext(
        run_id=7,
        collection_month="2026-08",
        message_id="m-1",
        document="abc",
        step="escalated extraction",
    )
    assert tracer.calls[0].step == "escalated extraction"
    assert tracer.calls[1].step == "extraction"
    assert tracer.calls[1].context.document is None
    assert outer.links == [tracer.calls[0].link, tracer.calls[1].link]
    assert outer.details() == {
        "trace_id": tracer.calls[1].link.trace_id,
        "trace_url": tracer.calls[1].link.url,
    }


def test_a_scope_with_no_traced_call_adds_nothing() -> None:
    with tracing.scope(step="classification") as scope:
        pass

    assert scope.details() == {}


def test_the_no_tracer_links_nothing() -> None:
    call = tracing.traced(NO_TRACER, "question", "m", "anthropic", lambda: 1, output=lambda _: {})

    with tracing.scope() as scope:
        assert call() == 1

    assert scope.links == []
    assert tracing.flush(NO_TRACER) is None


def test_flushing_a_tracer_that_fails_is_a_warning() -> None:
    assert tracing.flush(FakeTracer(failing=True)) == (
        "traces may not have been sent: TracingUnavailable"
    )


def test_a_trace_link_is_recorded_as_the_step_records_it() -> None:
    assert TraceLink("0" * 32, "https://lf/trace/x").details() == {
        "trace_id": "0" * 32,
        "trace_url": "https://lf/trace/x",
    }
    assert TraceLink("1" * 32).details() == {"trace_id": "1" * 32}


def test_content_is_the_input_of_a_call() -> None:
    assert Content(text="t").pdf is None


# --- The history of a billing document ------------------------------------------------------


def test_a_step_traced_again_is_not_a_new_step(tmp_path: Path) -> None:
    with closing(sqlite3.connect(tmp_path / "events.sqlite")) as db:
        db.executescript(trail.SCHEMA)
        at = datetime(2026, 9, 3, tzinfo=UTC)

        def read(trace_id: str) -> trail.Event:
            details = {"fields": {"vendor": "Slack"}, trail.TRACE_ID: trace_id}
            return trail.Event("read", "ops@x", "m-1", at, "h" * 64, "claude", details)

        trail.append(db, [read("a" * 32)])
        trail.append(db, [read("b" * 32)])
        stored = trail.events_of(db, "h" * 64)

    assert [e.event.details[trail.TRACE_ID] for e in stored] == ["a" * 32]
