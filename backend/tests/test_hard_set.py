"""The hard golden set: hand-written cases, kept apart from the samples, scored on their own.

No model is called: the eval runs with rules only, or with prepared answers.
"""

import hashlib
import json
from collections import Counter
from collections.abc import Generator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from invoice_collector.evals.documents import produce_documents
from invoice_collector.evals.golden import GoldenCase, load_golden, one_per_email
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
