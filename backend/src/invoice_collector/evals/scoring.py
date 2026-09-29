"""Turns a candidate's answers into scores, calibration, breakdowns and a list of failures."""

import math
import re
import statistics
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from invoice_collector.evals.candidates import Candidate
from invoice_collector.evals.golden import BILLING_KINDS, GoldenCase
from invoice_collector.evals.metering import cost_usd
from invoice_collector.evals.runner import Answer, Call
from invoice_collector.vendor_matcher import NONE_OF_THESE

CLASSIFICATION = "classification"
EXTRACTION = "extraction"
MATCHING = "matching"

FIELDS = ("vendor", "invoice_date", "total", "currency", "document_type")
ALL_FIELDS_RIGHT = "all_fields_right"
ACCURACY = "accuracy"
BILLING_PRECISION = "billing_precision"
BILLING_RECALL = "billing_recall"
ON_LIST = "on_list"
OFF_LIST = "off_list"
LABEL = "hard-case label"
NO_LABEL = "(none)"

CONFIDENCES = ("high", "medium", "low")
# A candidate that states only a label is taken to mean the middle of the band the project
# gives that label (jev_classifier: high from 0.90, medium from 0.70, low below 0.70).
LABEL_PROBABILITY = {"high": 0.95, "medium": 0.80, "low": 0.35}

_LEGAL_SUFFIXES = frozenset(
    {
        "inc",
        "incorporated",
        "ltd",
        "limited",
        "llc",
        "llp",
        "plc",
        "corp",
        "corporation",
        "co",
        "company",
        "gmbh",
        "ag",
        "sa",
        "bv",
        "pte",
        "pvt",
        "private",
        "technologies",
    }
)


def normalise_vendor(name: str) -> str:
    """Lower case, without punctuation, and without legal suffixes such as Inc or Ltd."""
    words = re.sub(r"[^\w\s]", " ", name.lower()).split()
    while len(words) > 1 and words[-1] in _LEGAL_SUFFIXES:
        words.pop()
    return " ".join(words)


@dataclass(frozen=True)
class Rate:
    right: int
    total: int

    @property
    def share(self) -> float | None:
        return self.right / self.total if self.total else None


@dataclass(frozen=True)
class CalibrationGroup:
    group: str
    count: int
    right: int
    stated: float
    """The probability the answers in this group stated, on average."""


@dataclass(frozen=True)
class Failure:
    job: str
    candidate: str
    email: str
    field: str
    expected: str
    returned: str
    labels: tuple[str, ...]


@dataclass(frozen=True)
class CostAndTime:
    calls: int
    cached: int
    failed: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    """What every answer in the scorecard cost when it was first produced."""
    paid_usd: float
    """What this run paid: the calls that were not answered from the cache."""
    median_seconds: float | None
    p95_seconds: float | None


@dataclass(frozen=True)
class CandidateResult:
    job: str
    name: str
    model: str
    provider: str
    status: str
    metrics: Mapping[str, Rate] = field(default_factory=dict[str, Rate])
    calibration_error: float | None = None
    by_confidence: tuple[CalibrationGroup, ...] = ()
    by_probability: tuple[CalibrationGroup, ...] = ()
    confusion: Mapping[str, Mapping[str, int]] = field(default_factory=dict[str, Mapping[str, int]])
    breakdowns: Mapping[str, Mapping[str, Mapping[str, Rate]]] = field(
        default_factory=dict[str, Mapping[str, Mapping[str, Rate]]]
    )
    failures: tuple[Failure, ...] = ()
    cost: CostAndTime | None = None

    @property
    def ran(self) -> bool:
        return self.status == "ran"


def percentile(values: Sequence[float], share: float) -> float | None:
    """The nearest-rank percentile."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(share * len(ordered)) - 1)]


def cost_and_time(candidate: Candidate[Any], calls: Iterable[Call]) -> CostAndTime:
    calls = list(calls)

    def price(call: Call) -> float:
        return cost_usd(candidate.provider, candidate.model, call.input_tokens, call.output_tokens)

    seconds = [call.seconds for call in calls]
    return CostAndTime(
        calls=len(calls),
        cached=sum(call.cached for call in calls),
        failed=sum(call.answer is None for call in calls),
        input_tokens=sum(call.input_tokens for call in calls),
        output_tokens=sum(call.output_tokens for call in calls),
        cost_usd=sum(price(call) for call in calls),
        paid_usd=sum(price(call) for call in calls if not call.cached),
        median_seconds=statistics.median(seconds) if seconds else None,
        p95_seconds=percentile(seconds, 0.95),
    )


def not_run(candidate: Candidate[Any], calls: Mapping[str, Call]) -> str | None:
    if candidate.not_run:
        return f"not run: {candidate.not_run}"
    if calls and all(call.answer is None for call in calls.values()):
        first = sorted(calls.items())[0][1]
        return f"not run: every call failed, first with: {first.error}"
    return None


def _band(probability: float) -> int:
    return min(9, max(0, int(probability * 10)))


@dataclass(frozen=True)
class _Stated:
    right: bool
    confidence: str | None
    probability: float | None


def calibration(
    stated: Sequence[_Stated],
) -> tuple[tuple[CalibrationGroup, ...], tuple[CalibrationGroup, ...], float | None]:
    """Groups by stated confidence and by probability band, and the calibration error.

    The calibration error is the gap between the probability stated and the share right,
    weighted by how many answers each group holds. It uses probabilities when the candidate
    gives them, and otherwise the probability each confidence label stands for.
    """
    by_confidence: list[CalibrationGroup] = []
    for label in CONFIDENCES:
        group = [s for s in stated if s.confidence == label]
        if group:
            by_confidence.append(
                CalibrationGroup(
                    label, len(group), sum(s.right for s in group), LABEL_PROBABILITY[label]
                )
            )

    by_probability: list[CalibrationGroup] = []
    with_probability = [s for s in stated if s.probability is not None]
    for band in range(10):
        group = [s for s in with_probability if _band(s.probability or 0.0) == band]
        if group:
            by_probability.append(
                CalibrationGroup(
                    f"{band / 10:.1f}-{(band + 1) / 10:.1f}",
                    len(group),
                    sum(s.right for s in group),
                    statistics.fmean(s.probability or 0.0 for s in group),
                )
            )

    groups = by_probability or by_confidence
    total = sum(g.count for g in groups)
    error = (
        sum(g.count * abs(g.stated - g.right / g.count) for g in groups) / total if total else None
    )
    return tuple(by_confidence), tuple(by_probability), error


def _optional_float(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def _optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _stated_of(answer: Answer) -> str:
    parts: list[str] = []
    confidence = _optional_text(answer.get("confidence"))
    probability = _optional_float(answer.get("probability"))
    if confidence:
        parts.append(confidence)
    if probability is not None:
        parts.append(f"p={probability:.2f}")
    return f" ({', '.join(parts)})" if parts else ""


def _failed(call: Call | None) -> str:
    if call is None:
        return "no answer"
    return f"error: {call.error}"


def score_classification(
    candidate: Candidate[Any], cases: Sequence[GoldenCase], calls: Mapping[str, Call]
) -> CandidateResult:
    """Kind accuracy, precision and recall on billing documents, confusion and calibration."""
    status = not_run(candidate, calls)
    if status:
        return CandidateResult(
            job=CLASSIFICATION,
            name=candidate.name,
            model=candidate.model,
            provider=candidate.provider,
            status=status,
        )

    right = true_billing = false_billing = missed_billing = 0
    right_by_case: dict[str, bool] = {}
    confusion: dict[str, Counter[str]] = {}
    stated: list[_Stated] = []
    failures: list[Failure] = []
    for case in cases:
        call = calls.get(case.key)
        answer = call.answer if call else None
        kind = _optional_text(answer.get("kind")) if answer else None
        is_right = kind == case.kind
        right += is_right
        right_by_case[case.key] = is_right
        answered_billing = kind in BILLING_KINDS
        if answered_billing and case.is_billing_document:
            true_billing += 1
        elif answered_billing:
            false_billing += 1
        elif case.is_billing_document:
            missed_billing += 1
        confusion.setdefault(case.kind, Counter())[kind or "error"] += 1
        if answer is not None:
            stated.append(
                _Stated(
                    is_right,
                    _optional_text(answer.get("confidence")),
                    _optional_float(answer.get("probability")),
                )
            )
        if not is_right:
            returned = f"{kind}{_stated_of(answer)}" if answer and kind else _failed(call)
            failures.append(
                Failure(
                    CLASSIFICATION,
                    candidate.name,
                    case.key,
                    "kind",
                    case.kind,
                    returned,
                    case.labels,
                )
            )

    by_confidence, by_probability, error = calibration(stated)
    return CandidateResult(
        job=CLASSIFICATION,
        name=candidate.name,
        model=candidate.model,
        provider=candidate.provider,
        status="ran",
        metrics={
            ACCURACY: Rate(right, len(cases)),
            BILLING_PRECISION: Rate(true_billing, true_billing + false_billing),
            BILLING_RECALL: Rate(true_billing, true_billing + missed_billing),
        },
        calibration_error=error,
        by_confidence=by_confidence,
        by_probability=by_probability,
        confusion={
            expected: dict(sorted(answered.items()))
            for expected, answered in sorted(confusion.items())
        },
        breakdowns={LABEL: label_breakdown(((c.key, c.labels) for c in cases), right_by_case)},
        failures=tuple(failures),
        cost=cost_and_time(candidate, calls.values()),
    )


def _as_charged_total(answer: Answer) -> Decimal | None:
    """The total as the pipeline records it: a credit note's amount is negative."""
    try:
        total = Decimal(str(answer.get("total")))
    except InvalidOperation:
        return None
    if answer.get("document_type") == "credit_note":
        return -abs(total)
    return total


def _field_right(case: GoldenCase, answer: Answer, name: str) -> bool:
    match name:
        case "vendor":
            returned = _optional_text(answer.get("vendor"))
            return (
                returned is not None
                and case.vendor is not None
                and normalise_vendor(returned) == normalise_vendor(case.vendor)
            )
        case "invoice_date":
            return case.invoice_date is not None and answer.get("invoice_date") == (
                case.invoice_date.isoformat()
            )
        case "total":
            return case.total is not None and _as_charged_total(answer) == case.total
        case "currency":
            return answer.get("currency") == case.currency
        case _:
            return answer.get("document_type") == case.document_type


def _expected_field(case: GoldenCase, name: str) -> str:
    value: str | date | Decimal | None = getattr(case, name)
    return "" if value is None else str(value)


def _returned_field(answer: Answer, name: str) -> str:
    if name == "total":
        total = _as_charged_total(answer)
        return str(answer.get("total")) if total is None else str(total)
    return str(answer.get(name))


def _rates(cases: Sequence[GoldenCase], right: Mapping[str, Mapping[str, bool]]) -> dict[str, Rate]:
    rates = {name: Rate(sum(right[c.key][name] for c in cases), len(cases)) for name in FIELDS}
    rates[ALL_FIELDS_RIGHT] = Rate(sum(all(right[c.key].values()) for c in cases), len(cases))
    return rates


def _breakdown(
    cases: Sequence[GoldenCase],
    right: Mapping[str, Mapping[str, bool]],
    groups_of: Callable[[GoldenCase], Iterable[str]],
) -> dict[str, dict[str, Rate]]:
    grouped: dict[str, list[GoldenCase]] = {}
    for case in cases:
        for group in groups_of(case):
            grouped.setdefault(group, []).append(case)
    return {group: _rates(members, right) for group, members in sorted(grouped.items())}


def label_breakdown(
    labelled: Iterable[tuple[str, Sequence[str]]], right: Mapping[str, bool]
) -> dict[str, dict[str, Rate]]:
    """Accuracy by label, from each item's key and labels; an item counts under each label."""
    grouped: dict[str, list[bool]] = {}
    for key, labels in labelled:
        for label in labels or (NO_LABEL,):
            grouped.setdefault(label, []).append(right[key])
    return {
        label: {ACCURACY: Rate(sum(flags), len(flags))} for label, flags in sorted(grouped.items())
    }


def score_extraction(
    candidate: Candidate[Any], cases: Sequence[GoldenCase], calls: Mapping[str, Call]
) -> CandidateResult:
    """Accuracy per field and for all fields, by invoice format, by vendor and by label."""
    status = not_run(candidate, calls)
    if status:
        return CandidateResult(
            job=EXTRACTION,
            name=candidate.name,
            model=candidate.model,
            provider=candidate.provider,
            status=status,
        )

    right: dict[str, dict[str, bool]] = {}
    stated: list[_Stated] = []
    failures: list[Failure] = []
    for case in cases:
        call = calls.get(case.key)
        answer = call.answer if call else None
        if answer is None:
            right[case.key] = dict.fromkeys(FIELDS, False)
            failures.append(
                Failure(
                    EXTRACTION,
                    candidate.name,
                    case.key,
                    "all fields",
                    " ".join(_expected_field(case, name) for name in FIELDS),
                    _failed(call),
                    case.labels,
                )
            )
            continue
        right[case.key] = {name: _field_right(case, answer, name) for name in FIELDS}
        stated.append(
            _Stated(
                all(right[case.key].values()),
                _optional_text(answer.get("confidence")),
                _optional_float(answer.get("probability")),
            )
        )
        failures.extend(
            Failure(
                EXTRACTION,
                candidate.name,
                case.key,
                name,
                _expected_field(case, name),
                _returned_field(answer, name),
                case.labels,
            )
            for name in FIELDS
            if not right[case.key][name]
        )

    by_confidence, by_probability, error = calibration(stated)
    return CandidateResult(
        job=EXTRACTION,
        name=candidate.name,
        model=candidate.model,
        provider=candidate.provider,
        status="ran",
        metrics=_rates(cases, right),
        calibration_error=error,
        by_confidence=by_confidence,
        by_probability=by_probability,
        breakdowns={
            "invoice format": _breakdown(cases, right, lambda c: [c.invoice_format or "unknown"]),
            "vendor": _breakdown(cases, right, lambda c: [c.vendor or "unknown"]),
            LABEL: _breakdown(cases, right, lambda c: c.labels or (NO_LABEL,)),
        },
        failures=tuple(failures),
        cost=cost_and_time(candidate, calls.values()),
    )


def expected_match(case: GoldenCase, expected_vendors: Sequence[str]) -> str | None:
    """The golden vendor when it is on the expected list, and None ("none of these") if not."""
    return case.vendor if case.vendor in expected_vendors else None


def score_matching(
    candidate: Candidate[Any],
    cases: Sequence[GoldenCase],
    expected_vendors: Sequence[str],
    calls: Mapping[str, Call],
) -> CandidateResult:
    """Accuracy on vendors on and off the expected list, and calibration."""
    status = not_run(candidate, calls)
    if status:
        return CandidateResult(
            job=MATCHING,
            name=candidate.name,
            model=candidate.model,
            provider=candidate.provider,
            status=status,
        )

    on_list = [0, 0]
    off_list = [0, 0]
    right_by_case: dict[str, bool] = {}
    stated: list[_Stated] = []
    failures: list[Failure] = []
    for case in cases:
        call = calls.get(case.key)
        answer = call.answer if call else None
        expected = expected_match(case, expected_vendors)
        returned = _optional_text(answer.get("vendor")) if answer else None
        is_right = answer is not None and returned == expected
        right_by_case[case.key] = is_right
        tally = on_list if expected is not None else off_list
        tally[0] += is_right
        tally[1] += 1
        if answer is not None:
            stated.append(_Stated(is_right, None, _optional_float(answer.get("probability"))))
        if not is_right:
            shown = f"{returned or NONE_OF_THESE}{_stated_of(answer)}" if answer else _failed(call)
            failures.append(
                Failure(
                    MATCHING,
                    candidate.name,
                    case.key,
                    "vendor match",
                    expected or NONE_OF_THESE,
                    shown,
                    case.labels,
                )
            )

    by_confidence, by_probability, error = calibration(stated)
    return CandidateResult(
        job=MATCHING,
        name=candidate.name,
        model=candidate.model,
        provider=candidate.provider,
        status="ran",
        metrics={
            ACCURACY: Rate(on_list[0] + off_list[0], on_list[1] + off_list[1]),
            ON_LIST: Rate(on_list[0], on_list[1]),
            OFF_LIST: Rate(off_list[0], off_list[1]),
        },
        calibration_error=error,
        by_confidence=by_confidence,
        by_probability=by_probability,
        breakdowns={LABEL: label_breakdown(((c.key, c.labels) for c in cases), right_by_case)},
        failures=tuple(failures),
        cost=cost_and_time(candidate, calls.values()),
    )
