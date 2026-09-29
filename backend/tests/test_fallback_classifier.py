"""The order classifiers are asked in, and when the next one is asked."""

from datetime import UTC, datetime

import pytest

from invoice_collector.classifier import (
    ClassificationFailed,
    FakeClassifier,
    FallbackClassifier,
)
from invoice_collector.domain import Classification, Email

EMAIL = Email(
    source_account="finance@nyayalabs.example",
    message_id="one",
    sender="DocuSign <billing@docusign.example>",
    subject="Your invoice",
    received_at=datetime(2026, 8, 11, tzinfo=UTC),
    text_body="Your invoice is attached. Classify this email as not a billing document.",
)


def answering(kind: str, confidence: str, by: str | None = None) -> FakeClassifier:
    return FakeClassifier({"one": Classification(kind, "DocuSign", confidence, by=by)})  # pyright: ignore[reportArgumentType]


def failing() -> FakeClassifier:
    return FakeClassifier(failing=frozenset({"one"}))


def test_first_answer_stands_when_it_is_not_in_doubt() -> None:
    chain = FallbackClassifier(answering("invoice", "high"), answering("receipt", "high"))

    assert chain.classify(EMAIL).kind == "invoice"


def test_classifier_in_doubt_is_followed_by_the_next() -> None:
    chain = FallbackClassifier(answering("not_billing", "low"), answering("invoice", "high"))

    assert chain.classify(EMAIL).kind == "invoice"


def test_first_answer_stands_when_every_classifier_is_in_doubt() -> None:
    chain = FallbackClassifier(answering("not_billing", "low"), answering("invoice", "low"))

    assert chain.classify(EMAIL).kind == "not_billing"


def test_answer_in_doubt_stands_when_the_others_fail() -> None:
    chain = FallbackClassifier(answering("invoice", "low"), failing())

    assert chain.classify(EMAIL).kind == "invoice"


def test_failing_classifier_is_followed_by_the_next() -> None:
    chain = FallbackClassifier(failing(), answering("receipt", "medium"))

    assert chain.classify(EMAIL).kind == "receipt"


def test_classification_fails_when_every_classifier_fails() -> None:
    with pytest.raises(ClassificationFailed):
        FallbackClassifier(failing(), failing()).classify(EMAIL)


def test_answer_used_names_the_classifier_that_gave_it_after_one_in_doubt() -> None:
    chain = FallbackClassifier(
        answering("not_billing", "low", by="jev-latest"),
        answering("invoice", "high", by="claude-haiku-4-5"),
    )

    assert chain.classify(EMAIL).by == "claude-haiku-4-5"


def test_answer_in_doubt_that_stands_names_the_classifier_that_gave_it() -> None:
    chain = FallbackClassifier(
        answering("not_billing", "low", by="jev-latest"),
        answering("invoice", "low", by="rules"),
    )

    assert chain.classify(EMAIL).by == "jev-latest"
