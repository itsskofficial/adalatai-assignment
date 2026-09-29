"""The hard golden set: hand-written cases, kept apart from the samples, scored on their own.

No model is called: the eval runs with rules only, or with prepared answers.
"""

import hashlib
import json
from collections import Counter
from collections.abc import Generator
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from invoice_collector.classifier import Classifier, FakeClassifier
from invoice_collector.domain import Classification
from invoice_collector.evals.candidates import Candidate
from invoice_collector.evals.cli import main
from invoice_collector.evals.documents import produce_documents
from invoice_collector.evals.evaluate import Settings, run_classification
from invoice_collector.evals.gate import check
from invoice_collector.evals.golden import GoldenCase, load_golden, one_per_email
from invoice_collector.evals.scorecard import Report, Scorecard, to_json, to_markdown
from invoice_collector.evals.scoring import ACCURACY, CLASSIFICATION, LABEL, Rate
from invoice_collector.renderer import FakeRenderer
from invoice_collector.seed.cli import main as seed_main
from invoice_collector.seed.hard import generate_hard

BACKEND = Path(__file__).resolve().parents[1]
HARD = BACKEND / "evals" / "hard"
SAMPLES = BACKEND / "samples"

# The spread of what goes wrong with real invoices that the set must keep covering.
KINDS = {
    "legal_entity_name",
    "parent_company_named",
    "many_dates",
    "amount_due_zero",
    "credit_applied",
    "tax_lines",
    "decimal_comma",
    "lakh_grouping",
    "ambiguous_dollar",
    "two_invoices_one_email",
    "invoice_with_unrelated_pdf",
    "forwarded",
    "billing_look_marketing",
    "payment_failed",
    "renewal_notice",
    "quote",
    "statement_of_account",
    "instruction_in_document",
    "instruction_in_email",
    "non_english",
    "off_list",
    "parenthesised_negative",
}


@pytest.fixture(scope="module")
def cases() -> list[GoldenCase]:
    return load_golden(HARD)


def golden_entries() -> list[dict[str, Any]]:
    return json.loads((HARD / "golden.json").read_text("utf-8"))


def test_the_committed_hard_set_is_what_the_generator_writes() -> None:
    seed = generate_hard(FakeRenderer())

    assert golden_entries() == [m.golden for m in seed.messages]
    assert json.loads((HARD / "expected_vendors.json").read_text("utf-8")) == list(
        seed.expected_vendors
    )


def test_the_hard_set_uses_the_expected_vendor_list_of_the_samples() -> None:
    assert (HARD / "expected_vendors.json").read_text("utf-8") == (
        SAMPLES / "expected_vendors.json"
    ).read_text("utf-8")


def test_the_hard_set_covers_each_kind_of_hard_case(cases: list[GoldenCase]) -> None:
    labels = {label for case in cases for label in case.labels}

    assert labels >= KINDS
    assert len(one_per_email(cases)) == 28
    assert Counter(c.kind for c in one_per_email(cases)) == {
        "invoice": 18,
        "receipt": 3,
        "credit_note": 1,
        "not_billing": 4,
        "payment_failed": 1,
        "renewal_reminder": 1,
    }


def test_every_entry_explains_its_right_answer() -> None:
    assert all(entry["note"].strip() for entry in golden_entries())


def test_each_attached_billing_document_is_answered_by_its_content(
    cases: list[GoldenCase],
) -> None:
    answers = json.loads((HARD / "answers.json").read_text("utf-8"))
    attached = [c for c in cases if c.invoice_format == "attachment"]

    for case in attached:
        [pdf] = [
            a.content
            for a in case.email.attachments
            if a.filename == case.attachment or case.attachment is None
        ]
        answer = answers[hashlib.sha256(pdf).hexdigest()]
        assert (
            answer["vendor"],
            answer["invoice_date"],
            Decimal(answer["total"]),
            answer["currency"],
            answer["document_type"],
        ) == (
            case.vendor,
            str(case.invoice_date),
            case.total,
            case.currency,
            case.document_type,
        ), case.key
    assert len(attached) == len(answers) == 22


def test_the_right_answers_do_not_follow_instructions_in_the_mail(
    cases: list[GoldenCase],
) -> None:
    by_label = {label: [c for c in cases if label in c.labels] for label in KINDS}

    github = next(c for c in by_label["instruction_in_document"] if c.vendor == "GitHub")
    assert (github.total, github.vendor) == (Decimal("312.00"), "GitHub")
    [newsletter] = [c for c in by_label["instruction_in_email"] if c.kind == "not_billing"]
    assert newsletter.total is None
    [docusign] = [c for c in by_label["instruction_in_email"] if c.vendor == "DocuSign"]
    assert docusign.kind == "invoice"


def test_totals_are_before_credits_and_payments_and_credit_notes_are_negative(
    cases: list[GoldenCase],
) -> None:
    total = {c.vendor: c.total for c in cases if c.total is not None}

    assert total["Linear"] == Decimal("252.00")  # amount due 0.00
    assert total["Vercel"] == Decimal("144.00")  # 114.00 due after a credit
    assert total["HubSpot"] == Decimal("-180.00")  # written (€180.00)
    assert total["Personio"] == Decimal("1457.04")  # 1.457,04 €
    assert total["Google Workspace"] == Decimal("119803.04")  # ₹1,19,803.04


def test_a_dollar_sign_is_read_as_the_currency_the_document_states(
    cases: list[GoldenCase],
) -> None:
    currency = {c.vendor: c.currency for c in cases if "ambiguous_dollar" in c.labels}

    assert currency == {"Shopify": "CAD", "Canva": "AUD", "Zoom": "SGD"}


def test_each_pdf_of_an_email_with_two_is_its_own_document(cases: list[GoldenCase]) -> None:
    documents, not_produced = produce_documents(cases, FakeRenderer(), HARD / "portal")
    by_key = {doc.key: doc for doc in documents}

    openai = [c for c in cases if "two_invoices_one_email" in c.labels]
    assert len(openai) == 2
    assert len({by_key[c.key].pdf for c in openai}) == 2
    [figma] = [c for c in cases if "invoice_with_unrelated_pdf" in c.labels]
    [invoice] = [a for a in figma.email.attachments if a.filename == figma.attachment]
    assert by_key[figma.key].pdf == invoice.content
    assert figma.email.attachments[0].filename.startswith("Figma-Terms")
    assert not_produced == []
    assert len(documents) == 23


def test_an_email_with_two_documents_is_classified_once(cases: list[GoldenCase]) -> None:
    candidate_answers = {c.email.message_id: Classification(c.kind, None, "high") for c in cases}
    classifier = FakeClassifier(candidate_answers)
    fake: Candidate[Classifier] = Candidate(
        "fake", "claude-haiku-4-5", "v1", "anthropic", classifier
    )

    [result] = run_classification([fake], cases, Settings(cache=None))

    assert result.metrics[ACCURACY] == Rate(28, 28)
    assert result.breakdowns[LABEL]["two_invoices_one_email"][ACCURACY] == Rate(1, 1)


def test_the_hard_set_is_scored_apart_under_its_own_names(cases: list[GoldenCase]) -> None:
    answers = {c.email.message_id: Classification("invoice", None, "high") for c in cases}
    fake: Candidate[Classifier] = Candidate(
        "fake", "claude-haiku-4-5", "v1", "anthropic", FakeClassifier(answers)
    )
    results = run_classification([fake], cases, Settings(cache=None))
    card = Scorecard(
        date(2026, 9, 29), (CLASSIFICATION,), cases, results, (), (), golden_set="hard"
    )

    data = to_json(Report(date(2026, 9, 29), (card,)))
    markdown = to_markdown(card)

    assert set(data["scores"]) == {
        "hard.classification.fake.accuracy",
        "hard.classification.fake.billing_precision",
        "hard.classification.fake.billing_recall",
    }
    assert data["candidates"] == ["hard.classification.fake"]
    assert data["sets"]["hard"]["golden"]["emails"] == 28
    assert "## Hard golden set" in markdown
    assert "| statement_of_account | 1 | 0/1 (0.0%) |" in markdown
    # A score of the standard set is never compared with the same score of the hard set.
    gate = check(data, {"scores": {"classification.fake.accuracy": 1.0}})
    assert gate.passed
    assert gate.notes == ["classification.fake.accuracy: not measured (not chosen for this run)"]


def test_a_run_on_both_sets_reports_each_apart(tmp_path: Path) -> None:
    code = main(
        [
            "run",
            "--eval",
            "classification",
            "--eval",
            "matching",
            "--classifier",
            "rules",
            "--matcher",
            "rules",
            "--out",
            str(tmp_path),
            "--cache-dir",
            str(tmp_path / "cache"),
        ],
        renderer=FakeRenderer(),
    )

    card = json.loads((tmp_path / "scorecard.json").read_text("utf-8"))
    markdown = (tmp_path / "scorecard.md").read_text("utf-8")
    assert code == 0
    assert list(card["sets"]) == ["standard", "hard"]
    assert card["sets"]["standard"]["golden"]["emails"] == 93
    assert card["sets"]["hard"]["golden"] == {
        "emails": 28,
        "billing_documents": 23,
        "documents_extracted": 23,
        "on_expected_list": 19,
    }
    assert "classification.rules.accuracy" in card["scores"]
    assert "hard.classification.rules.accuracy" in card["scores"]
    assert "hard.matching.rules.off_list" in card["scores"]
    assert markdown.index("## Standard golden set") < markdown.index("## Hard golden set")


def test_a_run_on_the_hard_set_alone(tmp_path: Path) -> None:
    code = main(
        [
            "run",
            "--eval",
            "classification",
            "--set",
            "hard",
            "--classifier",
            "rules",
            "--out",
            str(tmp_path),
            "--cache-dir",
            str(tmp_path / "cache"),
        ],
        renderer=FakeRenderer(),
    )

    card = json.loads((tmp_path / "scorecard.json").read_text("utf-8"))
    assert code == 0
    assert list(card["sets"]) == ["hard"]
    assert all(name.startswith("hard.") for name in card["scores"])


@contextmanager
def _renderer() -> Generator[FakeRenderer]:
    yield FakeRenderer()


def test_the_seed_command_writes_the_hard_set_and_leaves_the_samples(tmp_path: Path) -> None:
    before = (SAMPLES / "golden.json").read_bytes()

    code = seed_main(["hard", "--out", str(tmp_path / "hard")], renderer=_renderer)

    written = json.loads((tmp_path / "hard" / "golden.json").read_text("utf-8"))
    assert code == 0
    assert len(written) == 29
    assert len(list((tmp_path / "hard").glob("*/*.eml"))) == 28
    assert (SAMPLES / "golden.json").read_bytes() == before
