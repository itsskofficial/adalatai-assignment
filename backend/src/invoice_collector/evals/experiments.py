"""An eval run as experiments: one per candidate and dataset, with per-field scores.

When calls are traced (ADR 0017), each candidate's answers on a golden set, or on the
questions, are recorded as an experiment against a dataset of the golden cases: the case,
the right answer, the candidate's answer with its tokens, cost and time, and a score of 1
or 0 for each field it was scored on. The scores are the ones the scorecard counts, read
from its failures, so the experiment and the scorecard never disagree.

A dataset item holds the case's key, labels and invoice format. The email's text or the
question is added only when the tracer sends content. The golden sets are sample data,
but the rule is the same everywhere.
"""

from collections.abc import Mapping, Sequence
from typing import Any

from invoice_collector.classifier import text_of
from invoice_collector.evals.candidates import Candidate
from invoice_collector.evals.golden import GoldenCase
from invoice_collector.evals.questions import QUESTIONS, QuestionCase
from invoice_collector.evals.runner import Call
from invoice_collector.evals.scoring import (
    CLASSIFICATION,
    EXTRACTION,
    FIELDS,
    MATCHING,
    CandidateResult,
    expected_match,
)
from invoice_collector.tracing import Experiment, ExperimentItem, Tracer, record_experiment
from invoice_collector.tracing import cost_usd as priced
from invoice_collector.vendor_matcher import NONE_OF_THESE

DATASET_PREFIX = "invoice-collector"
ALL_RIGHT = "all_right"

# The fields each job is scored on, by the name the scorecard's failures give them.
SCORED_FIELDS: Mapping[str, tuple[str, ...]] = {
    CLASSIFICATION: ("kind",),
    EXTRACTION: FIELDS,
    MATCHING: ("vendor match",),
    QUESTIONS: ("choice",),
}
# A failure of an extraction that gave no answer at all.
_ALL_FIELDS = "all fields"


def dataset_name(job: str, golden_set: str | None) -> str:
    """invoice-collector/standard/extraction, or invoice-collector/questions."""
    return "/".join(p for p in (DATASET_PREFIX, golden_set, job) if p)


def _score_name(field: str) -> str:
    return field.replace(" ", "_")


def _scores(
    job: str, result: CandidateResult, case_of_failure: Mapping[str, str]
) -> dict[str, set[str]]:
    """The fields each case was answered wrong on, by case key."""
    wrong: dict[str, set[str]] = {}
    for failure in result.failures:
        key = case_of_failure.get(failure.email, failure.email)
        fields = SCORED_FIELDS[job] if failure.field == _ALL_FIELDS else (failure.field,)
        wrong.setdefault(key, set()).update(fields)
    return wrong


def _item(
    job: str,
    candidate: Candidate[Any],
    key: str,
    call: Call | None,
    wrong: set[str],
    about: Mapping[str, Any],
    expected: Mapping[str, Any],
) -> ExperimentItem:
    scores = {_score_name(f): 0.0 if f in wrong else 1.0 for f in SCORED_FIELDS[job]}
    scores[ALL_RIGHT] = 0.0 if wrong else 1.0
    if call is None:
        return ExperimentItem(key, about, expected, None, scores, error="no answer")
    return ExperimentItem(
        key=key,
        input=about,
        expected=expected,
        output=_answered(call.answer),
        scores=scores,
        error=call.error,
        input_tokens=call.input_tokens,
        output_tokens=call.output_tokens,
        seconds=call.seconds,
        cost_usd=(
            0.0
            if candidate.provider == "none"
            else priced(candidate.provider, candidate.model, call.input_tokens, call.output_tokens)
        ),
        cached=call.cached,
    )


def _answered(answer: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """A candidate's answer as an experiment keeps it: without a reason given in the model's
    own words for declining a question, which is never sent."""
    if answer is None:
        return None
    return {name: value for name, value in answer.items() if name != "reason"}


def _golden_input(case: GoldenCase, content: bool, text: str | None) -> dict[str, Any]:
    about: dict[str, Any] = {"case": case.key, "labels": list(case.labels)}
    if case.invoice_format is not None:
        about["invoice_format"] = case.invoice_format
    if content:
        about["subject"] = case.email.subject
        about["text"] = text if text is not None else text_of(case.email)
    return about


def _golden_expected(case: GoldenCase, job: str, expected_vendors: Sequence[str]) -> dict[str, Any]:
    if job == CLASSIFICATION:
        return {"kind": case.kind}
    if job == MATCHING:
        return {"vendor": expected_match(case, expected_vendors) or NONE_OF_THESE}
    return {
        name: "" if getattr(case, name) is None else str(getattr(case, name)) for name in FIELDS
    }


def record_golden(
    tracer: Tracer,
    job: str,
    golden_set: str,
    candidate: Candidate[Any],
    result: CandidateResult,
    cases: Sequence[GoldenCase],
    calls: Mapping[str, Call],
    *,
    run_label: str,
    expected_vendors: Sequence[str] = (),
    texts: Mapping[str, str] | None = None,
) -> None:
    """Records one candidate's run over a golden set as an experiment. Never raises."""
    if not result.ran or not calls:
        return
    wrong = _scores(job, result, {})
    items = [
        _item(
            job,
            candidate,
            case.key,
            calls.get(case.key),
            wrong.get(case.key, set()),
            _golden_input(case, tracer.sends_content, (texts or {}).get(case.key)),
            _golden_expected(case, job, expected_vendors),
        )
        for case in cases
    ]
    _record(tracer, job, golden_set, candidate, items, run_label)


def record_questions(
    tracer: Tracer,
    candidate: Candidate[Any],
    result: CandidateResult,
    cases: Sequence[QuestionCase],
    calls: Mapping[str, Call],
    *,
    run_label: str,
) -> None:
    """Records one candidate's run over the questions as an experiment. Never raises."""
    if not result.ran or not calls:
        return
    # The scorecard names a question's failure by the question itself.
    wrong = _scores(QUESTIONS, result, {case.question: case.key for case in cases})
    items: list[ExperimentItem] = []
    for case in cases:
        about: dict[str, Any] = {"case": case.key, "kinds": list(case.kinds)}
        if tracer.sends_content:
            about["question"] = case.question
        expected = {"choice": case.expected()}
        items.append(
            _item(
                QUESTIONS,
                candidate,
                case.key,
                calls.get(case.key),
                wrong.get(case.key, set()),
                about,
                expected,
            )
        )
    _record(tracer, QUESTIONS, None, candidate, items, run_label)


def _record(
    tracer: Tracer,
    job: str,
    golden_set: str | None,
    candidate: Candidate[Any],
    items: Sequence[ExperimentItem],
    run_label: str,
) -> None:
    metadata = {
        "job": job,
        "candidate": candidate.name,
        "model": candidate.model,
        "prompt_version": candidate.prompt_version,
    }
    if golden_set is not None:
        metadata["golden_set"] = golden_set
    run_name = " ".join(
        part for part in (candidate.name, candidate.prompt_version, run_label) if part
    )
    record_experiment(
        tracer,
        Experiment(
            dataset=dataset_name(job, golden_set),
            run_name=run_name,
            description=f"{job} by {candidate.name} ({candidate.model})",
            model=candidate.model,
            provider=candidate.provider,
            metadata=metadata,
            items=items,
        ),
    )
