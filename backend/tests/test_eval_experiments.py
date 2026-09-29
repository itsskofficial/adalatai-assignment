"""An eval run, recorded as experiments with per-field scores when calls are traced.

Candidates are fakes and the tracer is a fake: nothing is called. See ADR 0017.
"""

from dataclasses import replace
from pathlib import Path

import pytest
from test_evals import SETTINGS, case, classified, document, extraction, fake
from test_questions_eval import (
    AWS_AUGUST,
    FakeChooser,
    declined,
    question,
    small_set,
    total_spend,
)
from test_questions_eval import fake as fake_asker

from invoice_collector.classifier import FakeClassifier
from invoice_collector.evals import cli
from invoice_collector.evals.evaluate import (
    run_classification,
    run_extraction,
    run_matching,
    run_questions,
)
from invoice_collector.extractor import FakeExtractor
from invoice_collector.renderer import FakeRenderer
from invoice_collector.tracing import FakeTracer
from invoice_collector.vendor_matcher import FakeVendorMatcher, VendorMatch


def test_an_extraction_run_is_an_experiment_with_a_score_for_each_field() -> None:
    tracer = FakeTracer()
    right, wrong = case("a"), case("b", labels=("multi-page",))
    docs = [document(right), document(wrong)]
    extractor = FakeExtractor.for_documents(
        {docs[0].pdf: extraction(), docs[1].pdf: extraction(total="625.50")}
    )
    settings = replace(SETTINGS, tracer=tracer, golden_set="hard", run_label="2026-09-29")

    run_extraction([fake(extractor)], [right, wrong], docs, settings)

    [experiment] = tracer.experiments
    assert experiment.dataset == "invoice-collector/hard/extraction"
    assert experiment.run_name == "fake v1 2026-09-29"
    assert experiment.model == "claude-haiku-4-5"
    assert experiment.metadata["golden_set"] == "hard"
    first, second = experiment.items
    assert first.scores == {
        "vendor": 1.0,
        "invoice_date": 1.0,
        "total": 1.0,
        "currency": 1.0,
        "document_type": 1.0,
        "all_right": 1.0,
    }
    assert second.scores["total"] == 0.0
    assert second.scores["vendor"] == 1.0
    assert second.scores["all_right"] == 0.0
    assert second.expected["total"] == "652.50"
    assert second.output is not None and second.output["total"] == "625.50"
    assert second.input == {
        "case": "ops@example.test/b.eml",
        "labels": ["multi-page"],
        "invoice_format": "attachment",
    }


def test_the_text_of_a_case_is_in_its_dataset_item_only_when_content_is_sent() -> None:
    tracer = FakeTracer(sends_content=True)
    golden = case("a")
    doc = document(golden)
    extractor = FakeExtractor.for_documents({doc.pdf: extraction()})

    run_extraction([fake(extractor)], [golden], [doc], replace(SETTINGS, tracer=tracer))

    [item] = tracer.experiments[0].items
    assert item.input["text"] == doc.text


def test_a_failed_answer_scores_every_field_wrong() -> None:
    tracer = FakeTracer()
    read, unread = case("a"), case("b")
    docs = [document(read), document(unread)]
    extractor = FakeExtractor.for_documents({docs[0].pdf: extraction()})

    run_extraction([fake(extractor)], [read, unread], docs, replace(SETTINGS, tracer=tracer))

    failed = tracer.experiments[0].items[1]
    assert set(failed.scores.values()) == {0.0}
    assert failed.error is not None and failed.error.startswith("no prepared answer")


def test_classification_and_matching_runs_are_experiments_too() -> None:
    tracer = FakeTracer()
    settings = replace(SETTINGS, tracer=tracer)
    cases = [case("s", vendor="Slack"), case("n", vendor="Notion")]
    docs = [document(c) for c in cases]
    classifier = FakeClassifier({"s": classified("invoice"), "n": classified("receipt")})
    matcher = FakeVendorMatcher(
        {docs[0].text: VendorMatch("Slack", 0.98), docs[1].text: VendorMatch("Slack", 0.6)}
    )

    run_classification([fake(classifier)], cases, settings)
    run_matching([fake(matcher)], cases, docs, ["Slack", "Notion"], settings)

    classification, matching = tracer.experiments
    assert classification.dataset == "invoice-collector/standard/classification"
    assert [i.scores["kind"] for i in classification.items] == [1.0, 0.0]
    assert matching.dataset == "invoice-collector/standard/matching"
    assert [i.scores["vendor_match"] for i in matching.items] == [1.0, 0.0]
    assert matching.items[1].expected == {"vendor": "Notion"}


def test_a_questions_run_is_an_experiment_scored_on_the_choice() -> None:
    tracer = FakeTracer()
    cases = small_set(
        question("right", "AWS in August?", "total_spend", AWS_AUGUST, "plain"),
        question("wrong", "AWS last month?", "total_spend", AWS_AUGUST, "relative_date"),
    )
    chooser = FakeChooser(
        {"AWS in August?": total_spend(), "AWS last month?": total_spend(vendor="Slack")}
    )

    run_questions([fake_asker(chooser)], cases, replace(SETTINGS, tracer=tracer))

    [experiment] = tracer.experiments
    assert experiment.dataset == "invoice-collector/questions"
    assert [(i.key, i.scores["choice"]) for i in experiment.items] == [
        ("right", 1.0),
        ("wrong", 0.0),
    ]
    # The question itself is content, sent only when asked for.
    assert "question" not in experiment.items[0].input


def test_nothing_is_recorded_without_a_tracer() -> None:
    golden = case("a")
    doc = document(golden)
    extractor = FakeExtractor.for_documents({doc.pdf: extraction()})

    [result] = run_extraction([fake(extractor)], [golden], [doc], SETTINGS)

    assert result.ran


def test_a_tracer_that_fails_does_not_fail_the_eval() -> None:
    golden = case("a")
    doc = document(golden)
    extractor = FakeExtractor.for_documents({doc.pdf: extraction()})
    settings = replace(SETTINGS, tracer=FakeTracer(failing=True))

    [result] = run_extraction([fake(extractor)], [golden], [doc], settings)

    assert result.ran


def test_the_eval_command_records_its_run_and_sends_it_before_it_ends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracer = FakeTracer()
    monkeypatch.setattr(cli.tracing, "tracer_from_environment", lambda: tracer)

    exit_code = cli.main(
        ["run", "--eval", "classification", "--set", "standard", "--classifier", "rules"]
        + ["--out", str(tmp_path / "out"), "--cache-dir", str(tmp_path / "cache")],
        renderer=FakeRenderer(),
    )

    assert exit_code == 0
    [experiment] = tracer.experiments
    assert experiment.dataset == "invoice-collector/standard/classification"
    assert experiment.run_name.startswith("rules ")
    assert tracer.flushes == 1


def test_a_reason_given_in_the_model_s_words_is_not_recorded() -> None:
    tracer = FakeTracer()
    cases = small_set(question("declined", "AWS next month?", "cannot_answer", {}, "future"))
    chooser = FakeChooser({"AWS next month?": declined("I cannot see the future")})

    run_questions([fake_asker(chooser)], cases, replace(SETTINGS, tracer=tracer))

    [item] = tracer.experiments[0].items
    assert item.output == {"query": "cannot_answer", "parameters": {}}
    assert item.scores["choice"] == 1.0
