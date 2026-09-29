"""Decides which expected vendor a billing document belongs to, if any."""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from invoice_collector.jev_classifier import (
    DEFAULT_BASE_URL,
    DEFAULT_MAX_RETRIES,
    DEFAULT_MODEL,
    DEFAULT_TIMEOUT_SECONDS,
    MAX_CHOICE_OPTIONS,
    JevFailed,
    ask_choice,
    cut_to_fit,
    jev_client,
)

NONE_OF_THESE = "none of these"
MAX_EXPECTED_VENDORS = MAX_CHOICE_OPTIONS - 1  # one option is kept for "none of these"

INSTRUCTIONS = "Which vendor issued this billing document?"
NONE_OF_THESE_MEANS = "The document was issued by a vendor that is not among the other options."


class VendorMatchFailed(Exception):
    """The billing document could not be matched against the expected vendors."""


@dataclass(frozen=True)
class VendorMatch:
    vendor: str | None
    """The expected vendor, or None when the document is from a vendor not on the list."""
    probability: float | None = None


class VendorMatcher(Protocol):
    def match(self, text: str, expected_vendors: Sequence[str]) -> VendorMatch:
        """Raises VendorMatchFailed."""
        ...


def named_vendors(expected_vendors: Sequence[str]) -> list[str]:
    """The expected vendors that have a name, each once, in the order given."""
    return list(dict.fromkeys(v.strip() for v in expected_vendors if v.strip()))


class RuleVendorMatcher:
    """Matches when the text names exactly one expected vendor."""

    def match(self, text: str, expected_vendors: Sequence[str]) -> VendorMatch:
        named = [
            vendor
            for vendor in named_vendors(expected_vendors)
            if re.search(rf"(?<!\w){re.escape(vendor)}(?!\w)", text, re.I)
        ]
        return VendorMatch(named[0] if len(named) == 1 else None)


class RulesFirstVendorMatcher:
    """Rules decide when they can. Each model is asked in turn only when they cannot.

    The rules decline when the text names no expected vendor, or more than one. A model
    that fails hands the question to the next; when every model fails, VendorMatchFailed
    says why, and the rules' answer, that there is no match, stands. See ADR 0009.
    """

    def __init__(self, *models: VendorMatcher) -> None:
        self.models = models
        self._rules = RuleVendorMatcher()

    def match(self, text: str, expected_vendors: Sequence[str]) -> VendorMatch:
        decided = self._rules.match(text, expected_vendors)
        if decided.vendor is not None or not self.models or not expected_vendors:
            return decided
        failures: list[str] = []
        for model in self.models:
            try:
                return model.match(text, expected_vendors)
            except VendorMatchFailed as failure:
                failures.append(str(failure))
        raise VendorMatchFailed("; then ".join(failures))


class JevVendorMatcher:
    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        self._client = jev_client(api_key, base_url, timeout, max_retries)
        self._model = model

    def match(self, text: str, expected_vendors: Sequence[str]) -> VendorMatch:
        vendors = named_vendors(expected_vendors)
        if not vendors:
            return VendorMatch(None)
        if len(vendors) > MAX_EXPECTED_VENDORS:
            raise VendorMatchFailed(
                f"Jev can choose among at most {MAX_EXPECTED_VENDORS} expected vendors, "
                f"got {len(vendors)}"
            )
        if NONE_OF_THESE in vendors:
            raise VendorMatchFailed(f"an expected vendor cannot be named {NONE_OF_THESE!r}")

        options: dict[str, str | None] = dict.fromkeys(vendors)
        options[NONE_OF_THESE] = NONE_OF_THESE_MEANS
        try:
            choice, probability = ask_choice(
                self._client,
                self._model,
                cut_to_fit(text, INSTRUCTIONS, *options, NONE_OF_THESE_MEANS),
                "vendor",
                INSTRUCTIONS,
                options,
            )
        except JevFailed as failure:
            raise VendorMatchFailed(str(failure)) from failure
        return VendorMatch(None if choice == NONE_OF_THESE else choice, probability)


class FakeVendorMatcher:
    """Returns prepared answers by text, and matches the rest by rules."""

    def __init__(
        self,
        answers: Mapping[str, VendorMatch] | None = None,
        failing: frozenset[str] = frozenset(),
    ) -> None:
        self._answers = dict(answers or {})
        self._failing = failing
        self._rules = RuleVendorMatcher()

    def match(self, text: str, expected_vendors: Sequence[str]) -> VendorMatch:
        if text in self._failing:
            raise VendorMatchFailed("the vendor matcher is unavailable")
        return self._answers.get(text) or self._rules.match(text, expected_vendors)
