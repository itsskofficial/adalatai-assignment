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
    MAX_STATE_CHARACTERS,
    JevFailed,
    ask_choice,
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


class RuleVendorMatcher:
    """Matches when the text names exactly one expected vendor."""

    def match(self, text: str, expected_vendors: Sequence[str]) -> VendorMatch:
        named = [
            vendor
            for vendor in dict.fromkeys(expected_vendors)
            if re.search(rf"(?<!\w){re.escape(vendor)}(?!\w)", text, re.I)
        ]
        return VendorMatch(named[0] if len(named) == 1 else None)


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
        vendors = list(dict.fromkeys(expected_vendors))
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
                text[:MAX_STATE_CHARACTERS],
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
