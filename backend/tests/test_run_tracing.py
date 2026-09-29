"""Tests at the run seam: each model call a run makes is traced, and linked from the history.

The classifier, extractors and vendor matcher are fakes that trace their calls as a
model's adapter does, to a fake tracer. See ADR 0017.
"""

from collections.abc import Sequence
from dataclasses import replace
from datetime import date

from support import AUGUST, ENGINEERING, SONNET, Collection, usd
from test_run_checks import SLACK_PDF, slack_email
from test_run_trail import SLACK, SLACK_HASH, entry, trail_of

from invoice_collector.tracing import FakeTracer, TraceContext
from invoice_collector.vendor_matcher import VendorMatch


class _ModelMatcher:
    """Matches every name to AWS, as a model would that the rules could not help."""

    def match(self, text: str, expected_vendors: Sequence[str]) -> VendorMatch:
        return VendorMatch("AWS", 0.95, "claude-haiku-4-5")


def test_each_model_call_of_a_run_is_traced_with_what_it_was_about(
    collection: Collection,
) -> None:
    tracer = FakeTracer()
    collection.tracer = tracer
    collection.answers[SLACK_PDF] = SLACK
    collection.expect("Slack", usual="640.00")
    email = slack_email()

    result = collection.run([email])

    about = TraceContext(
        run_id=result.run_id,
        collection_month=str(AUGUST),
        source_account=ENGINEERING,
        message_id=email.message_id,
        invoice_format="attachment",
    )
    assert [(c.step, c.model, c.context) for c in tracer.calls] == [
        ("classification", "claude-haiku-4-5", replace(about, step="classification")),
        (
            "extraction",
            "claude-haiku-4-5",
            replace(about, step="extraction", document=SLACK_HASH),
        ),
    ]
    extraction = tracer.calls[1].finished
    assert extraction is not None
    assert (extraction.input_tokens, extraction.output_tokens) == (1000, 50)
    assert extraction.cost_usd is not None and extraction.cost_usd > 0
    assert extraction.output is not None and extraction.output["total"] == "652.50"


def test_the_history_links_each_step_to_the_trace_of_its_model_call(
    collection: Collection,
) -> None:
    tracer = FakeTracer()
    collection.tracer = tracer
    collection.answers[SLACK_PDF] = SLACK

    collection.run([slack_email()])

    history = trail_of(collection)
    classified, read = tracer.calls[0].link, tracer.calls[1].link
    assert entry(history, "classified").details["trace_id"] == classified.trace_id
    assert entry(history, "classified").details["trace_url"] == classified.url
    assert entry(history, "read").details["trace_id"] == read.trace_id
    assert "trace_id" not in entry(history, "checked").details


def test_a_reading_done_again_is_traced_as_escalated(collection: Collection) -> None:
    tracer = FakeTracer()
    collection.tracer = tracer
    collection.answers[SLACK_PDF] = replace(SLACK, confidence="low")
    collection.stronger_answers[SLACK_PDF] = replace(SLACK, by=SONNET)

    collection.run([slack_email()])

    [again] = [c for c in tracer.calls if c.step == "escalated extraction"]
    assert again.model == SONNET
    assert again.context.document == SLACK_HASH
    assert entry(trail_of(collection), "read_again").details["trace_id"] == again.link.trace_id


def test_a_vendor_matched_by_a_model_is_traced_and_linked(collection: Collection) -> None:
    tracer = FakeTracer()
    collection.tracer = tracer
    collection.vendor_matcher = _ModelMatcher()
    collection.expect("AWS")
    collection.answers[SLACK_PDF] = usd("Amazon Web Services", date(2026, 8, 3), "90.00")

    collection.run([slack_email()])

    [matching] = [c for c in tracer.calls if c.step == "vendor matching"]
    assert matching.context.document == SLACK_HASH
    matched = entry(trail_of(collection), "matched")
    assert matched.details["trace_id"] == matching.link.trace_id
    assert matched.details["expected_vendor"] == "AWS"


def test_a_tracer_that_fails_never_fails_a_run(collection: Collection) -> None:
    collection.tracer = FakeTracer(failing=True)
    collection.answers[SLACK_PDF] = SLACK

    result = collection.run([slack_email()])

    assert [(row.vendor, str(row.total)) for row in result.summary] == [("Slack", "652.50")]
    assert "trace_id" not in entry(trail_of(collection), "read").details


def test_a_run_with_no_tracer_records_no_trace(collection: Collection) -> None:
    collection.answers[SLACK_PDF] = SLACK

    collection.run([slack_email()])

    assert all("trace_id" not in e.details for e in trail_of(collection).entries)
