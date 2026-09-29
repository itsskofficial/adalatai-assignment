"""The offline eval, scored with fake candidates on a tiny golden dataset. No model is called."""

import json
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from conftest import ReplayServer

from invoice_collector.classifier import FakeClassifier
from invoice_collector.claude_classifier import ClaudeClassifier
from invoice_collector.domain import (
    Attachment,
    Classification,
    Confidence,
    DocumentType,
    Email,
    EmailKind,
    Extraction,
)
from invoice_collector.evals.candidates import (
    NO_KEY,
    Candidate,
    classifier_candidate,
    extractor_candidate,
    matcher_candidate,
)
from invoice_collector.evals.cli import main
from invoice_collector.evals.documents import Document, produce_documents
from invoice_collector.evals.evaluate import (
    Settings,
    run_classification,
    run_extraction,
    run_matching,
)
from invoice_collector.evals.gate import check
from invoice_collector.evals.golden import GoldenCase
from invoice_collector.evals.metering import measuring, metered_anthropic
from invoice_collector.evals.recommend import adr_0009
from invoice_collector.evals.runner import AnswerCache, Call, Item, judge_all
from invoice_collector.evals.scorecard import Scorecard, to_json, to_markdown
from invoice_collector.evals.scoring import (
    ACCURACY,
    CLASSIFICATION,
    MATCHING,
    CandidateResult,
    Rate,
    normalise_vendor,
)
from invoice_collector.extractor import FakeExtractor
from invoice_collector.renderer import FakeRenderer
from invoice_collector.vendor_matcher import FakeVendorMatcher, VendorMatch

RECORDED = Path(__file__).parent / "recorded"


def email(message_id: str, subject: str = "Your invoice", body: str = "") -> Email:
    return Email(
        source_account="ops@example.test",
        message_id=message_id,
        sender="Billing <billing@vendor.example>",
        subject=subject,
        received_at=datetime(2026, 8, 3, tzinfo=UTC),
        text_body=body or subject,
    )


def case(
    message_id: str,
    kind: EmailKind = "invoice",
    *,
    vendor: str = "Slack",
    invoice_format: str = "attachment",
    labels: tuple[str, ...] = (),
    total: str = "652.50",
    currency: str = "USD",
) -> GoldenCase:
    billing = kind in ("invoice", "receipt", "credit_note")
    return GoldenCase(
        source_account="ops@example.test",
        file_name=f"{message_id}.eml",
        email=email(message_id),
        kind=kind,
        labels=labels,
        invoice_format=invoice_format if billing else None,
        vendor=vendor if billing else None,
        invoice_date=date(2026, 8, 3) if billing else None,
        total=Decimal(total) if billing else None,
        currency=currency if billing else None,
        document_type="credit_note" if kind == "credit_note" else ("invoice" if billing else None),
    )


def fake[J](judge: J, name: str = "fake", version: str = "v1") -> Candidate[J]:
    return Candidate(name, "claude-haiku-4-5", version, "anthropic", judge)


def classified(
    kind: EmailKind, confidence: Confidence = "high", probability: float | None = None
) -> Classification:
    return Classification(kind, None, confidence, probability)


def extraction(
    vendor: str = "Slack",
    total: str = "652.50",
    currency: str = "USD",
    document_type: DocumentType = "invoice",
) -> Extraction:
    return Extraction(
        document_type=document_type,
        vendor=vendor,
        invoice_date=date(2026, 8, 3),
        total=Decimal(total),
        currency=currency,
    )


def document(golden: GoldenCase) -> Document:
    pdf = f"%PDF- {golden.key}".encode()
    return Document(golden.key, pdf, golden.key, f"text of {golden.key}", 1)


SETTINGS = Settings(cache=None)


# --- Extraction ---------------------------------------------------------------------------------


def test_vendor_names_match_ignoring_case_punctuation_and_legal_suffixes() -> None:
    assert normalise_vendor("Slack Technologies, LLC") == normalise_vendor("slack")
    assert normalise_vendor("Zoho Corporation Pvt. Ltd.") == normalise_vendor("Zoho")
    assert normalise_vendor("Notion Labs, Inc.") == "notion labs"
    assert normalise_vendor("Amazon Web Services") != normalise_vendor("AWS")


def test_extraction_is_scored_per_field() -> None:
    right, wrong = case("a"), case("b")
    docs = [document(right), document(wrong)]
    extractor = FakeExtractor.for_documents(
        {
            docs[0].pdf: extraction(vendor="Slack Technologies, Inc."),
            docs[1].pdf: extraction(total="625.50", currency="EUR"),
        }
    )

    [result] = run_extraction([fake(extractor)], [right, wrong], docs, SETTINGS)

    assert result.metrics["vendor"] == Rate(2, 2)
    assert result.metrics["total"] == Rate(1, 2)
    assert result.metrics["currency"] == Rate(1, 2)
    assert result.metrics["all_fields_right"] == Rate(1, 2)
    assert [(f.email, f.field, f.expected, f.returned) for f in result.failures] == [
        ("ops@example.test/b.eml", "total", "652.50", "625.50"),
        ("ops@example.test/b.eml", "currency", "USD", "EUR"),
    ]


def test_a_credit_note_is_compared_with_its_negative_total() -> None:
    credit = case("c", "credit_note", total="-30.25")
    doc = document(credit)
    extractor = FakeExtractor.for_documents(
        {doc.pdf: extraction(total="30.25", document_type="credit_note")}
    )

    [result] = run_extraction([fake(extractor)], [credit], [doc], SETTINGS)

    assert result.metrics["total"] == Rate(1, 1)


def test_a_failed_extraction_gets_every_field_wrong() -> None:
    read, unread = case("a"), case("b")
    docs = [document(read), document(unread)]
    extractor = FakeExtractor.for_documents({docs[0].pdf: extraction()})

    [result] = run_extraction([fake(extractor)], [read, unread], docs, SETTINGS)

    assert result.metrics["all_fields_right"] == Rate(1, 2)
    assert result.metrics["vendor"] == Rate(1, 2)
    [failure] = result.failures
    assert (failure.field, failure.expected) == (
        "all fields",
        "Slack 2026-08-03 652.50 USD invoice",
    )
    assert failure.returned.startswith("error: no prepared answer")


def test_a_candidate_whose_every_call_failed_is_reported_as_not_run() -> None:
    golden = case("a")

    [result] = run_extraction([fake(FakeExtractor({}))], [golden], [document(golden)], SETTINGS)

    assert (
        result.status
        == "not run: every call failed, first with: no prepared answer for this document"
    )
    assert result.metrics == {}


def test_extraction_is_broken_down_by_format_vendor_and_label() -> None:
    cases = [
        case("a", invoice_format="attachment", vendor="Slack"),
        case("b", invoice_format="body", vendor="Notion", labels=("foreign_currency",)),
        case("c", invoice_format="body", vendor="Notion", labels=("foreign_currency",)),
    ]
    docs = [document(c) for c in cases]
    extractor = FakeExtractor.for_documents(
        {
            docs[0].pdf: extraction(),
            docs[1].pdf: extraction(vendor="Notion"),
            docs[2].pdf: extraction(vendor="Notion", currency="EUR"),
        }
    )

    [result] = run_extraction([fake(extractor)], cases, docs, SETTINGS)

    by = {
        dim: {g: r["all_fields_right"] for g, r in groups.items()}
        for dim, groups in result.breakdowns.items()
    }
    assert by["invoice format"] == {"attachment": Rate(1, 1), "body": Rate(1, 2)}
    assert by["vendor"] == {"Notion": Rate(1, 2), "Slack": Rate(1, 1)}
    assert by["hard-case label"] == {"(none)": Rate(1, 1), "foreign_currency": Rate(1, 2)}


# --- Classification -----------------------------------------------------------------------------


def test_precision_and_recall_on_billing_documents_are_reported_separately() -> None:
    cases = [case("i1"), case("i2"), case("r1", "receipt"), case("n1", "not_billing")]
    answers = {
        "i1": classified("invoice"),
        "i2": classified("receipt"),  # still a billing document, but the wrong kind
        "r1": classified("not_billing"),  # a missed billing document
        "n1": classified("invoice"),  # a false billing document
    }

    [result] = run_classification([fake(FakeClassifier(answers))], cases, SETTINGS)

    assert result.metrics[ACCURACY] == Rate(1, 4)
    assert result.metrics["billing_precision"] == Rate(2, 3)
    assert result.metrics["billing_recall"] == Rate(2, 3)


def test_the_confusion_table_counts_each_answer_against_the_golden_kind() -> None:
    cases = [case("i1"), case("i2"), case("n1", "not_billing")]
    answers = {
        "i1": classified("invoice"),
        "i2": classified("receipt"),
        "n1": classified("not_billing"),
    }

    [result] = run_classification([fake(FakeClassifier(answers))], cases, SETTINGS)

    assert result.confusion == {
        "invoice": {"invoice": 1, "receipt": 1},
        "not_billing": {"not_billing": 1},
    }


def test_a_failed_classification_is_wrong_and_shown_as_an_error() -> None:
    cases = [case("i1"), case("i2")]
    classifier = FakeClassifier({"i2": classified("invoice")}, failing=frozenset({"i1"}))

    [result] = run_classification([fake(classifier)], cases, SETTINGS)

    assert result.metrics[ACCURACY] == Rate(1, 2)
    assert result.metrics["billing_recall"] == Rate(1, 2)
    assert result.confusion == {"invoice": {"error": 1, "invoice": 1}}
    assert [f.returned for f in result.failures] == ["error: the classifier is unavailable"]


def test_calibration_groups_answers_by_confidence_and_by_probability_band() -> None:
    cases = [case("a"), case("b"), case("c"), case("d")]
    answers = {
        "a": classified("invoice", "high", 0.97),
        "b": classified("receipt", "high", 0.93),
        "c": classified("invoice", "medium", 0.75),
        "d": classified("invoice", "low", 0.31),
    }

    [result] = run_classification([fake(FakeClassifier(answers))], cases, SETTINGS)

    assert [(g.group, g.count, g.right) for g in result.by_confidence] == [
        ("high", 2, 1),
        ("medium", 1, 1),
        ("low", 1, 1),
    ]
    assert [(g.group, g.count, g.right) for g in result.by_probability] == [
        ("0.3-0.4", 1, 1),
        ("0.7-0.8", 1, 1),
        ("0.9-1.0", 2, 1),
    ]
    # |0.31-1|*1 + |0.75-1|*1 + |0.95-0.5|*2, over 4 answers
    assert result.calibration_error == pytest.approx((0.69 + 0.25 + 0.90) / 4)


def test_a_candidate_stating_only_labels_is_calibrated_by_what_the_labels_stand_for() -> None:
    cases = [case("a"), case("b")]
    answers = {"a": classified("invoice", "high"), "b": classified("invoice", "high")}

    [result] = run_classification([fake(FakeClassifier(answers))], cases, SETTINGS)

    assert result.by_probability == ()
    assert result.calibration_error == pytest.approx(0.05)


# --- Vendor matching ----------------------------------------------------------------------------


def test_vendor_matching_is_scored_on_and_off_the_expected_list() -> None:
    cases = [case("s", vendor="Slack"), case("n", vendor="Notion"), case("l", vendor="Loom")]
    docs = [document(c) for c in cases]
    matcher = FakeVendorMatcher(
        {
            docs[0].text: VendorMatch("Slack", 0.98),
            docs[1].text: VendorMatch("Slack", 0.55),
            docs[2].text: VendorMatch(None, 0.9),
        }
    )

    [result] = run_matching([fake(matcher)], cases, docs, ["Slack", "Notion"], SETTINGS)

    assert result.metrics[ACCURACY] == Rate(2, 3)
    assert result.metrics["on_list"] == Rate(1, 2)
    assert result.metrics["off_list"] == Rate(1, 1)
    assert [(f.expected, f.returned) for f in result.failures] == [("Notion", "Slack (p=0.55)")]
    assert [(g.group, g.count) for g in result.by_probability] == [("0.5-0.6", 1), ("0.9-1.0", 2)]


# --- The cache ----------------------------------------------------------------------------------


class CountingClassifier:
    def __init__(self) -> None:
        self.calls = 0

    def classify(self, email: Email) -> Classification:
        self.calls += 1
        return classified("invoice")


def ask(classifier: CountingClassifier, content: Email) -> dict[str, Any]:
    return {"kind": classifier.classify(content).kind}


def test_the_cache_returns_a_stored_answer_without_calling_the_candidate(tmp_path: Path) -> None:
    cache = AnswerCache(tmp_path)
    classifier = CountingClassifier()
    items = [Item("a", "content-a", email("a"))]

    first = judge_all(fake(classifier), items, ask, cache)
    second = judge_all(fake(classifier), items, ask, cache)

    assert classifier.calls == 1
    assert second["a"].answer == first["a"].answer == {"kind": "invoice"}
    assert second["a"].cached and not first["a"].cached


def test_the_cache_misses_when_the_prompt_version_changes(tmp_path: Path) -> None:
    cache = AnswerCache(tmp_path)
    classifier = CountingClassifier()
    items = [Item("a", "content-a", email("a"))]

    judge_all(fake(classifier, version="v1"), items, ask, cache)
    judge_all(fake(classifier, version="v2"), items, ask, cache)

    assert classifier.calls == 2


def test_no_cache_asks_again_and_still_stores_the_answer(tmp_path: Path) -> None:
    cache = AnswerCache(tmp_path)
    classifier = CountingClassifier()
    items = [Item("a", "content-a", email("a"))]

    judge_all(fake(classifier), items, ask, cache, read_cache=False)
    judge_all(fake(classifier), items, ask, cache, read_cache=False)
    judge_all(fake(classifier), items, ask, cache)

    assert classifier.calls == 2


def test_a_stored_answer_that_cannot_be_read_is_asked_for_again(tmp_path: Path) -> None:
    cache = AnswerCache(tmp_path)
    key = AnswerCache.key(fake(CountingClassifier()), "content-a")
    (tmp_path / f"{key}.json").write_text('{"answer": {"kind": "inv', "utf-8")

    assert cache.get(key) is None


def test_storing_an_answer_leaves_nothing_else_in_the_cache(tmp_path: Path) -> None:
    cache = AnswerCache(tmp_path)
    key = AnswerCache.key(fake(CountingClassifier()), "content-a")

    cache.put(key, Call(answer={"kind": "invoice"}, input_tokens=1, output_tokens=1, seconds=0.1))
    cache.put(key, Call(answer={"kind": "receipt"}, input_tokens=1, output_tokens=1, seconds=0.1))

    assert [p.name for p in tmp_path.iterdir()] == [f"{key}.json"]
    stored = cache.get(key)
    assert stored is not None and stored.answer == {"kind": "receipt"}


def test_a_cached_answer_keeps_the_tokens_and_time_of_the_call(tmp_path: Path) -> None:
    cache = AnswerCache(tmp_path)
    key = AnswerCache.key(fake(CountingClassifier()), "content-a")
    cache.put(key, Call({"kind": "invoice"}, input_tokens=800, output_tokens=30, seconds=1.5))

    stored = cache.get(key)

    assert stored is not None
    assert (stored.input_tokens, stored.output_tokens, stored.seconds) == (800, 30, 1.5)


# --- Candidates ---------------------------------------------------------------------------------


def test_a_candidate_without_its_key_is_reported_as_not_run() -> None:
    haiku = classifier_candidate("claude-haiku", {})
    jev = matcher_candidate("jev", {})
    sonnet = extractor_candidate("claude-sonnet", {}, ("Slack",))

    [result] = run_classification([haiku], [case("a")], SETTINGS)

    assert (haiku.not_run, jev.not_run, sonnet.not_run) == (NO_KEY, NO_KEY, NO_KEY)
    assert result.status == "not run: no key"
    assert sonnet.model == "claude-sonnet-5-5"
    assert haiku.model == "claude-haiku-4-5"


def test_claude_usage_is_read_from_the_response(replay_server: ReplayServer) -> None:
    recorded = json.loads((RECORDED / "claude_payment_failed.json").read_text("utf-8"))
    client = metered_anthropic("not-a-real-key", replay_server(200, recorded), max_retries=0)

    with measuring() as usage:
        ClaudeClassifier(client).classify(email("a", "Your payment failed"))

    assert (usage.input_tokens, usage.output_tokens) == (491, 24)


# --- Documents ----------------------------------------------------------------------------------


def test_each_invoice_format_becomes_a_pdf_without_a_network(tmp_path: Path) -> None:
    (tmp_path / "in_abc.html").write_text("<h1>Invoice</h1>", "utf-8")
    (tmp_path / "sign-in.html").write_text('<input type="password">', "utf-8")

    link = '<a href="http://localhost:8765/{}">View invoice</a>'
    attachment = Attachment("i.pdf", "application/pdf", b"%PDF-1")
    cases = [
        replace(case("pdf"), email=replace(email("pdf"), attachments=(attachment,))),
        replace(case("body"), email=email("body", body="Total paid: $12.00")),
        replace(
            case("portal"),
            email=replace(email("portal"), html_body=link.format("in_abc.html"), text_body=None),
        ),
        replace(
            case("gated"),
            email=replace(email("gated"), html_body=link.format("sign-in.html"), text_body=None),
        ),
        case("news", "not_billing"),
    ]

    documents, not_produced = produce_documents(cases, FakeRenderer(), tmp_path)

    assert {d.key: d.pdf[:14] for d in documents} == {
        "ops@example.test/pdf.eml": b"%PDF-1",
        "ops@example.test/body.eml": b"%PDF-rendered ",
        "ops@example.test/portal.eml": b"%PDF-rendered ",
    }
    assert [(n.key, n.reason) for n in not_produced] == [
        ("ops@example.test/gated.eml", "login-gated portal link: needs an assisted download")
    ]


def test_the_attachment_a_golden_case_names_is_the_one_extracted(tmp_path: Path) -> None:
    terms = Attachment("terms.pdf", "application/pdf", b"%PDF-terms")
    invoice = Attachment("invoice.pdf", "application/pdf", b"%PDF-invoice")
    both = replace(email("both"), attachments=(terms, invoice))
    cases = [
        replace(case("both"), email=both, attachment="invoice.pdf"),
        replace(case("both"), email=both, attachment="missing.pdf"),
    ]

    documents, not_produced = produce_documents(cases, FakeRenderer(), tmp_path)

    assert [(d.key, d.pdf) for d in documents] == [
        ("ops@example.test/both.eml#invoice.pdf", b"%PDF-invoice")
    ]
    assert [(n.key, n.reason) for n in not_produced] == [
        ("ops@example.test/both.eml#missing.pdf", "no PDF attachment named missing.pdf")
    ]


# --- The scorecard ------------------------------------------------------------------------------


def test_the_scorecard_lists_every_failure_with_its_labels() -> None:
    cases = [
        case("i1", labels=("multi_page",)),
        case("n1", "not_billing", labels=("promotion_with_price",)),
        case("n2", "not_billing"),
    ]
    answers = {
        "i1": classified("receipt"),
        "n1": classified("invoice"),
        "n2": classified("not_billing"),
    }
    results = run_classification([fake(FakeClassifier(answers))], cases, SETTINGS)
    card = Scorecard(date(2026, 9, 29), (CLASSIFICATION,), cases, results, (), ("Slack",))

    markdown = to_markdown(card)

    assert "| fake | ops@example.test/i1.eml | kind | invoice | receipt (high) | multi_page |" in (
        markdown
    )
    assert "| fake | ops@example.test/n1.eml | kind | not_billing | invoice (high) | " in markdown
    assert "n2.eml" not in markdown
    assert to_json(card)["results"][0]["failures"][1]["labels"] == ["promotion_with_price"]


def test_the_scorecard_is_the_same_for_the_same_answers() -> None:
    cases = [case("b"), case("a")]
    answers = {"a": classified("invoice"), "b": classified("receipt")}

    def card() -> str:
        results = run_classification([fake(FakeClassifier(answers))], cases, SETTINGS)
        return to_markdown(Scorecard(date(2026, 9, 29), (CLASSIFICATION,), cases, results, (), ()))

    assert card() == card()
    assert card().count("2026-09-29") == 1


# --- The regression gate ------------------------------------------------------------------------

BASELINE = {
    "scores": {
        "classification.claude-haiku.accuracy": 0.90,
        "classification.claude-haiku.billing_recall": 1.0,
        "extraction.claude-haiku.all_fields_right": 0.80,
    }
}


def scorecard_with(**scores: float) -> dict[str, Any]:
    return {
        "scores": {**BASELINE["scores"], **{k.replace("__", "."): v for k, v in scores.items()}}
    }


def test_the_gate_passes_on_equal_or_better_scores() -> None:
    assert check(scorecard_with(), BASELINE).passed
    assert check(
        scorecard_with(**{"extraction__claude-haiku__all_fields_right": 0.9}), BASELINE
    ).passed


def test_the_gate_fails_naming_each_metric_that_fell() -> None:
    card = scorecard_with(
        **{
            "extraction__claude-haiku__all_fields_right": 0.79,
            "classification__claude-haiku__billing_recall": 0.98,
        }
    )

    result = check(card, BASELINE)

    assert not result.passed
    assert [fall.split(" ")[0] for fall in result.falls] == [
        "classification.claude-haiku.billing_recall",
        "extraction.claude-haiku.all_fields_right",
    ]


def test_the_gate_allows_a_small_fall_where_a_tolerance_allows_it() -> None:
    card = scorecard_with(**{"classification__claude-haiku__accuracy": 0.88})

    assert check(card, BASELINE).passed  # accuracy may fall by the default 0.05
    assert not check(card, BASELINE, {"accuracy": 0.01}).passed


def test_the_gate_notes_a_candidate_not_run_for_want_of_a_key_or_not_chosen() -> None:
    card = {
        "candidates": [
            "classification.claude-haiku",
            "extraction.claude-haiku",
            "extraction.rules",
        ],
        "scores": {"classification.claude-haiku.accuracy": 0.9},
        "not_run": {"extraction.claude-haiku": "not run: no key"},
    }
    baseline = {
        "scores": {
            "classification.claude-haiku.accuracy": 0.9,
            "extraction.claude-haiku.all_fields_right": 0.8,
            "extraction.claude-sonnet.all_fields_right": 0.9,
            "extraction.rules.all_fields_right": 0.8,
        }
    }

    result = check(card, baseline)

    assert result.notes == [
        "extraction.claude-haiku.all_fields_right: not measured (not run: no key)",
        "extraction.claude-sonnet.all_fields_right: not measured (not chosen for this run)",
    ]
    assert result.falls == [
        "extraction.rules.all_fields_right: not measured (missing from the scorecard)"
    ]


def test_check_and_accept_on_the_command_line(tmp_path: Path) -> None:
    scorecard, baseline = tmp_path / "scorecard.json", tmp_path / "baseline.json"
    scorecard.write_text(json.dumps(scorecard_with()), "utf-8")
    paths = ["--scorecard", str(scorecard), "--baseline", str(baseline)]

    assert main(["accept", *paths]) == 0
    assert json.loads(baseline.read_text("utf-8")) == {
        "scores": dict(sorted(BASELINE["scores"].items()))
    }
    assert main(["check", *paths]) == 0

    scorecard.write_text(
        json.dumps(scorecard_with(**{"classification__claude-haiku__billing_recall": 0.5})), "utf-8"
    )
    assert main(["check", *paths]) == 1


def test_a_run_over_the_committed_samples_writes_the_scorecard(tmp_path: Path) -> None:
    code = main(
        [
            "run",
            "--eval",
            "classification",
            "--classifier",
            "rules",
            "--classifier",
            "jev",
            "--out",
            str(tmp_path),
            "--cache-dir",
            str(tmp_path / "cache"),
        ],
        renderer=FakeRenderer(),
    )

    card = json.loads((tmp_path / "scorecard.json").read_text("utf-8"))
    assert code == 0
    assert card["not_run"] == {"classification.jev": "not run: no key"}
    assert card["golden"]["emails"] == 93
    assert "classification.rules.accuracy" in card["scores"]
    assert (tmp_path / "scorecard.md").read_text("utf-8").startswith("# Offline eval scorecard")


# --- ADR 0009 -----------------------------------------------------------------------------------


def result(
    name: str, accuracy: Rate, calibration_error: float | None, status: str = "ran"
) -> CandidateResult:
    return CandidateResult(
        CLASSIFICATION,
        name,
        name,
        "anthropic",
        status,
        metrics={ACCURACY: accuracy} if status == "ran" else {},
        calibration_error=calibration_error,
    )


@pytest.mark.parametrize(
    ("haiku", "jev", "choice"),
    [
        (result("claude-haiku", Rate(90, 100), 0.10), result("jev", Rate(90, 100), 0.03), "jev"),
        (result("claude-haiku", Rate(90, 100), 0.10), result("jev", Rate(95, 100), 0.03), "jev"),
        (
            result("claude-haiku", Rate(90, 100), 0.10),
            result("jev", Rate(89, 100), 0.01),
            "claude-haiku",
        ),
        (
            result("claude-haiku", Rate(90, 100), 0.10),
            result("jev", Rate(90, 100), 0.10),
            "claude-haiku",
        ),
        (
            result("claude-haiku", Rate(90, 100), 0.10),
            result("jev", Rate(0, 0), None, "not run: no key"),
            "claude-haiku",
        ),
        (
            result("claude-haiku", Rate(0, 0), None, "not run: no key"),
            result("jev", Rate(90, 100), 0.03),
            "undecided",
        ),
    ],
    ids=[
        "jev-matches-and-is-better-calibrated",
        "jev-more-accurate-and-better-calibrated",
        "jev-less-accurate",
        "jev-not-better-calibrated",
        "jev-not-run",
        "haiku-not-run",
    ],
)
def test_the_adr_0009_rule(haiku: CandidateResult, jev: CandidateResult, choice: str) -> None:
    assert adr_0009(CLASSIFICATION, [haiku, jev]).choice == choice


def test_the_adr_0009_rule_is_undecided_without_a_claude_haiku_candidate() -> None:
    jev = CandidateResult(
        MATCHING, "jev", "jev-latest", "jev", "ran", {ACCURACY: Rate(9, 10)}, 0.02
    )

    recommendation = adr_0009(MATCHING, [jev])

    assert recommendation.choice == "undecided"
    assert "Claude Haiku was not scored" in recommendation.reason


def test_the_adr_0009_rule_warns_when_no_mistake_was_made_to_calibrate_against() -> None:
    haiku = result("claude-haiku", Rate(10, 10), 0.05)
    jev = result("jev", Rate(10, 10), 0.003)

    recommendation = adr_0009(CLASSIFICATION, [haiku, jev])

    assert recommendation.choice == "jev"
    assert "Neither made a mistake" in recommendation.reason
