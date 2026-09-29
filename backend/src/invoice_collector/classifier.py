"""Decides what kind of email this is: a billing document, a billing signal, or neither."""

import re
from collections.abc import Mapping
from email.utils import parseaddr
from typing import Protocol

from invoice_collector.domain import Classification, Email, EmailKind


class ClassificationFailed(Exception):
    """The email could not be classified."""


class Classifier(Protocol):
    def classify(self, email: Email) -> Classification:
        """Raises ClassificationFailed."""
        ...


# Order matters: the first rule that matches decides the kind.
_RULES: tuple[tuple[EmailKind, re.Pattern[str]], ...] = (
    (
        "payment_failed",
        re.compile(
            r"payment (?:has )?(?:failed|was declined|declined|was unsuccessful|unsuccessful)"
            r"|(?:could not|couldn't|unable to) (?:process|charge|collect)"
            r"|card (?:was|has been) declined",
            re.I,
        ),
    ),
    (
        "renewal_reminder",
        re.compile(
            r"(?:will|is set to|is about to|is due to) (?:auto-?)?renew"
            r"|renews on|upcoming renewal|renewal (?:reminder|notice)",
            re.I,
        ),
    ),
    ("credit_note", re.compile(r"\bcredit (?:note|memo)\b|\brefund(?:ed)?\b", re.I)),
    (
        "receipt",
        re.compile(r"\breceipt\b|payment (?:received|confirmation)|thanks for your payment", re.I),
    ),
    ("invoice", re.compile(r"\binvoice\b|billing statement", re.I)),
)


def text_of(email: Email) -> str:
    """Everything in the email that words can be read from."""
    body = email.text_body or re.sub(r"<[^>]+>", " ", email.html_body or "")
    names = " ".join(a.filename for a in email.attachments)
    return f"{email.subject}\n{body}\n{names}"


def vendor_from_sender(email: Email) -> str | None:
    name, _ = parseaddr(email.sender)
    name = re.sub(r"\b(billing|team|support|accounts?|payments?)\b", "", name, flags=re.I)
    return name.strip(" -,") or None


class RuleClassifier:
    def classify(self, email: Email) -> Classification:
        text = text_of(email)
        for kind, pattern in _RULES:
            if pattern.search(text):
                return Classification(kind, vendor_from_sender(email), "low")
        return Classification("not_billing", None, "low")


class FakeClassifier:
    """Returns prepared answers by message id, and classifies the rest by rules."""

    def __init__(
        self,
        answers: Mapping[str, Classification] | None = None,
        failing: frozenset[str] = frozenset(),
    ) -> None:
        self._answers = dict(answers or {})
        self._failing = failing
        self._rules = RuleClassifier()

    def classify(self, email: Email) -> Classification:
        if email.message_id in self._failing:
            raise ClassificationFailed("the classifier is unavailable")
        return self._answers.get(email.message_id) or self._rules.classify(email)


class FallbackClassifier:
    """Tries each classifier in turn until one classifies the email."""

    def __init__(self, *classifiers: Classifier) -> None:
        self._classifiers = classifiers

    def classify(self, email: Email) -> Classification:
        failures: list[str] = []
        for classifier in self._classifiers:
            try:
                return classifier.classify(email)
            except ClassificationFailed as failure:
                failures.append(str(failure))
        raise ClassificationFailed("; then ".join(failures))
