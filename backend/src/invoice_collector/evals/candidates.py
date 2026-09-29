"""The models and rules the eval can score, each under a name chosen on the command line."""

import hashlib
import inspect
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Protocol

from typesafe_sdk import RetryPolicy, TypeSafeClient

from invoice_collector import (
    classifier,
    claude_classifier,
    claude_extractor,
    claude_vendor_matcher,
    jev_classifier,
    rule_extractor,
    vendor_matcher,
)
from invoice_collector.api import questions
from invoice_collector.api.questions import Answerer, Choice, Ledgered
from invoice_collector.classifier import Classifier, RuleClassifier
from invoice_collector.claude_classifier import ClaudeClassifier
from invoice_collector.claude_extractor import ClaudeExtractor
from invoice_collector.claude_vendor_matcher import ClaudeVendorMatcher
from invoice_collector.evals.metering import Provider, metered_anthropic, metered_jev_http_client
from invoice_collector.extractor import Extractor
from invoice_collector.jev_classifier import (
    DEFAULT_BASE_URL,
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT_SECONDS,
    JevClassifier,
)
from invoice_collector.rule_extractor import RuleExtractor
from invoice_collector.vendor_matcher import JevVendorMatcher, RuleVendorMatcher, VendorMatcher

CLAUDE_MODELS = {"claude-haiku": "claude-haiku-4-5", "claude-sonnet": "claude-sonnet-5-5"}
JEV_MODEL = jev_classifier.DEFAULT_MODEL
RULES = "rules"

CLASSIFIERS = ("claude-haiku", "claude-sonnet", "jev", RULES)
EXTRACTORS = ("claude-haiku", "claude-sonnet", RULES)
MATCHERS = ("claude-haiku", "claude-sonnet", "jev", RULES)
ASKERS = ("claude-haiku", "claude-sonnet")

NO_KEY = "no key"


class QueryChooser(Protocol):
    """Chooses the fixed query for a question in plain words, as Ask your invoices does."""

    def choose(self, question: str, ledger: Ledgered) -> Choice:
        """The fixed query chosen for the question, checked, or why none can run."""
        ...


@dataclass(frozen=True)
class Candidate[J]:
    """One model or set of rules doing one job, as the eval scores it."""

    name: str
    model: str
    prompt_version: str
    provider: Provider
    judge: J | None
    not_run: str | None = None
    """Why the candidate was not run, when it was not."""

    @property
    def cached(self) -> bool:
        """Answers that cost money are kept, so an unchanged pair is never paid for twice."""
        return self.provider != "none"


def version_of(*modules: ModuleType) -> str:
    """Changes whenever the prompt, the answer schema or the reading of the answer changes."""
    digest = hashlib.sha256()
    for module in modules:
        digest.update(inspect.getsource(module).encode())
    return digest.hexdigest()[:16]


class _MeteredJevClassifier(JevClassifier):
    def __init__(self, api_key: str) -> None:
        super().__init__(api_key)
        self._client.close()
        self._client = _metered_jev_client(api_key)


class _MeteredJevVendorMatcher(JevVendorMatcher):
    def __init__(self, api_key: str) -> None:
        super().__init__(api_key)
        self._client.close()
        self._client = _metered_jev_client(api_key)


def _metered_jev_client(api_key: str) -> TypeSafeClient:
    return TypeSafeClient(
        api_key=api_key,
        base_url=DEFAULT_BASE_URL,
        retry=RetryPolicy(max_retries=DEFAULT_MAX_RETRIES),
        http_client=metered_jev_http_client(DEFAULT_TIMEOUT_SECONDS),
    )


def _unknown(name: str, known: Sequence[str]) -> ValueError:
    return ValueError(f"unknown candidate {name!r}; choose from {', '.join(known)}")


def classifier_candidate(name: str, env: Mapping[str, str]) -> Candidate[Classifier]:
    if name in CLAUDE_MODELS:
        model, version = CLAUDE_MODELS[name], version_of(claude_classifier, classifier)
        key = env.get("ANTHROPIC_API_KEY")
        if not key:
            return Candidate(name, model, version, "anthropic", None, NO_KEY)
        return Candidate(
            name, model, version, "anthropic", ClaudeClassifier(metered_anthropic(key), model)
        )
    if name == "jev":
        version = version_of(jev_classifier, classifier)
        key = env.get("JEV_API_KEY")
        if not key:
            return Candidate(name, JEV_MODEL, version, "jev", None, NO_KEY)
        return Candidate(name, JEV_MODEL, version, "jev", _MeteredJevClassifier(key))
    if name == RULES:
        return Candidate(name, RULES, version_of(classifier), "none", RuleClassifier())
    raise _unknown(name, CLASSIFIERS)


def extractor_candidate(
    name: str, env: Mapping[str, str], known_vendors: tuple[str, ...]
) -> Candidate[Extractor]:
    if name in CLAUDE_MODELS:
        model, version = CLAUDE_MODELS[name], version_of(claude_extractor)
        key = env.get("ANTHROPIC_API_KEY")
        if not key:
            return Candidate(name, model, version, "anthropic", None, NO_KEY)
        return Candidate(
            name, model, version, "anthropic", ClaudeExtractor(metered_anthropic(key), model)
        )
    if name == RULES:
        return Candidate(
            name, RULES, version_of(rule_extractor), "none", RuleExtractor(known_vendors)
        )
    raise _unknown(name, EXTRACTORS)


def matcher_candidate(name: str, env: Mapping[str, str]) -> Candidate[VendorMatcher]:
    if name in CLAUDE_MODELS:
        model = CLAUDE_MODELS[name]
        version = version_of(claude_vendor_matcher, vendor_matcher)
        key = env.get("ANTHROPIC_API_KEY")
        if not key:
            return Candidate(name, model, version, "anthropic", None, NO_KEY)
        return Candidate(
            name, model, version, "anthropic", ClaudeVendorMatcher(metered_anthropic(key), model)
        )
    if name == "jev":
        version = version_of(vendor_matcher, jev_classifier)
        key = env.get("JEV_API_KEY")
        if not key:
            return Candidate(name, JEV_MODEL, version, "jev", None, NO_KEY)
        return Candidate(name, JEV_MODEL, version, "jev", _MeteredJevVendorMatcher(key))
    if name == RULES:
        return Candidate(name, RULES, version_of(vendor_matcher), "none", RuleVendorMatcher())
    raise _unknown(name, MATCHERS)


def asker_candidate(name: str, env: Mapping[str, str], today: date) -> Candidate[QueryChooser]:
    """Ask your invoices on one Claude model, with today fixed so relative dates have one answer.

    Only the choice is asked for, so nothing is run and no unanswered question is logged.
    """
    if name not in CLAUDE_MODELS:
        raise _unknown(name, ASKERS)
    model, version = CLAUDE_MODELS[name], version_of(questions)
    key = env.get("ANTHROPIC_API_KEY")
    if not key:
        return Candidate(name, model, version, "anthropic", None, NO_KEY)
    answerer: QueryChooser = Answerer(
        metered_anthropic(key), Path(os.devnull), lambda: today, model
    )
    return Candidate(name, model, version, "anthropic", answerer)
