"""The scorecard: the same results as Markdown for people and as JSON for the gate.

Each golden set is reported on its own, and the questions eval after them. Scores of the
standard set keep their plain names (classification.jev.accuracy), so the accepted baseline
still reads; scores of the hard set are prefixed hard., and those of the questions eval
questions., so no two sets are ever compared with each other by the gate.
"""

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from invoice_collector.evals.documents import NotProduced
from invoice_collector.evals.golden import STANDARD, GoldenCase, one_per_email
from invoice_collector.evals.questions import (
    ANSWERABLE,
    KIND,
    QUERY,
    QUESTIONS,
    UNANSWERABLE,
    QuestionSet,
)
from invoice_collector.evals.recommend import Recommendation, adr_0009
from invoice_collector.evals.scoring import (
    ACCURACY,
    ALL_FIELDS_RIGHT,
    BILLING_PRECISION,
    BILLING_RECALL,
    CLASSIFICATION,
    EXTRACTION,
    FIELDS,
    LABEL,
    MATCHING,
    OFF_LIST,
    ON_LIST,
    CalibrationGroup,
    CandidateResult,
    Failure,
    Rate,
)

GOLDEN_JOBS = (CLASSIFICATION, EXTRACTION, MATCHING)
JOBS = (*GOLDEN_JOBS, QUESTIONS)
JOB_TITLES = {
    CLASSIFICATION: "Classification",
    EXTRACTION: "Extraction",
    MATCHING: "Vendor matching",
    QUESTIONS: "Ask your invoices",
}
SET_TITLES = {STANDARD: "Standard golden set", "hard": "Hard golden set"}
SET_SOURCES = {
    STANDARD: "the sample mail in `backend/samples`, generated with its answers",
    "hard": "the cases real invoices get wrong, in `backend/evals/hard`",
}
METRIC_TITLES = {
    ACCURACY: "Accuracy",
    BILLING_PRECISION: "Billing document precision",
    BILLING_RECALL: "Billing document recall",
    "vendor": "Vendor",
    "invoice_date": "Invoice date",
    "total": "Total",
    "currency": "Currency",
    "document_type": "Document type",
    ALL_FIELDS_RIGHT: "All fields right",
    ON_LIST: "On the expected list",
    OFF_LIST: "Off the expected list (none of these)",
    QUERY: "Right query, parameters aside",
    ANSWERABLE: "Answerable, answered right",
    UNANSWERABLE: "Not answerable, declined",
}
METRICS = {
    CLASSIFICATION: (ACCURACY, BILLING_PRECISION, BILLING_RECALL),
    EXTRACTION: (*FIELDS, ALL_FIELDS_RIGHT),
    MATCHING: (ACCURACY, ON_LIST, OFF_LIST),
    QUESTIONS: (ACCURACY, QUERY, ANSWERABLE, UNANSWERABLE),
}


def _scores(results: Sequence[CandidateResult], prefix: str) -> dict[str, float]:
    return {
        f"{prefix}{r.job}.{r.name}.{metric}": share
        for r in results
        if r.ran
        for metric, rate in r.metrics.items()
        if (share := rate.share) is not None
    }


def _paid(results: Sequence[CandidateResult]) -> dict[str, float]:
    paid: dict[str, float] = {}
    for r in results:
        if r.cost is not None and r.provider != "none":
            paid[r.provider] = paid.get(r.provider, 0.0) + r.cost.paid_usd
    return dict(sorted(paid.items()))


@dataclass(frozen=True)
class Scorecard:
    """The results of one golden set."""

    run_date: date
    jobs: tuple[str, ...]
    cases: Sequence[GoldenCase]
    results: Sequence[CandidateResult]
    not_produced: Sequence[NotProduced]
    expected_vendors: Sequence[str]
    golden_set: str = STANDARD

    @property
    def prefix(self) -> str:
        """Before each score's name: nothing for the standard set, so the baseline still reads."""
        return "" if self.golden_set == STANDARD else f"{self.golden_set}."

    def of(self, job: str) -> list[CandidateResult]:
        return [r for r in self.results if r.job == job]

    @property
    def recommendations(self) -> list[Recommendation]:
        return [
            adr_0009(job, self.results) for job in (CLASSIFICATION, MATCHING) if job in self.jobs
        ]

    @property
    def scores(self) -> dict[str, float]:
        """Every headline score of every candidate that ran, for the regression gate."""
        return _scores(self.results, self.prefix)

    @property
    def paid_this_run(self) -> dict[str, float]:
        return _paid(self.results)

    def write(self, folder: Path) -> tuple[Path, Path]:
        return Report(self.run_date, (self,)).write(folder)


@dataclass(frozen=True)
class QuestionsCard:
    """The results of the questions eval."""

    questions: QuestionSet
    results: Sequence[CandidateResult]

    @property
    def scores(self) -> dict[str, float]:
        return _scores(self.results, "")


@dataclass(frozen=True)
class Report:
    """Everything one run of the eval measured: each golden set, then the questions."""

    run_date: date
    cards: Sequence[Scorecard] = field(default_factory=tuple[Scorecard, ...])
    questions: QuestionsCard | None = None

    @property
    def results(self) -> list[CandidateResult]:
        found = [r for card in self.cards for r in card.results]
        return found + (list(self.questions.results) if self.questions else [])

    @property
    def scores(self) -> dict[str, float]:
        scores: dict[str, float] = {}
        for card in self.cards:
            scores.update(card.scores)
        if self.questions:
            scores.update(self.questions.scores)
        return scores

    @property
    def paid_this_run(self) -> dict[str, float]:
        return _paid(self.results)

    def write(self, folder: Path) -> tuple[Path, Path]:
        folder.mkdir(parents=True, exist_ok=True)
        markdown, data = folder / "scorecard.md", folder / "scorecard.json"
        markdown.write_text(to_markdown(self), "utf-8")
        data.write_text(json.dumps(to_json(self), indent=2) + "\n", "utf-8")
        return markdown, data


def _report(scored: "Scorecard | Report") -> Report:
    return scored if isinstance(scored, Report) else Report(scored.run_date, (scored,))


# --- JSON ----------------------------------------------------------------------------------------


def _rate_json(rate: Rate) -> dict[str, Any]:
    return {"right": rate.right, "total": rate.total, "share": rate.share}


def _groups_json(groups: Sequence[CalibrationGroup]) -> list[dict[str, Any]]:
    return [
        {"group": g.group, "count": g.count, "right": g.right, "stated": g.stated} for g in groups
    ]


def _failure_json(f: Failure) -> dict[str, Any]:
    return {
        "email": f.email,
        "field": f.field,
        "expected": f.expected,
        "returned": f.returned,
        "labels": list(f.labels),
    }


def _result_json(r: CandidateResult) -> dict[str, Any]:
    cost = r.cost
    return {
        "job": r.job,
        "candidate": r.name,
        "model": r.model,
        "provider": r.provider,
        "status": r.status,
        "metrics": {name: _rate_json(rate) for name, rate in r.metrics.items()},
        "calibration_error": r.calibration_error,
        "calibration_by_confidence": _groups_json(r.by_confidence),
        "calibration_by_probability": _groups_json(r.by_probability),
        "confusion": {k: dict(v) for k, v in r.confusion.items()},
        "breakdowns": {
            dimension: {
                group: {name: _rate_json(rate) for name, rate in rates.items()}
                for group, rates in groups.items()
            }
            for dimension, groups in r.breakdowns.items()
        },
        "cost_and_time": None
        if cost is None
        else {
            "calls": cost.calls,
            "cached": cost.cached,
            "failed": cost.failed,
            "input_tokens": cost.input_tokens,
            "output_tokens": cost.output_tokens,
            "cost_usd": round(cost.cost_usd, 6),
            "median_seconds": cost.median_seconds,
            "p95_seconds": cost.p95_seconds,
        },
        "failures": [_failure_json(f) for f in r.failures],
    }


def _card_json(card: Scorecard) -> dict[str, Any]:
    return {
        "golden": _golden_counts(card),
        "results": [_result_json(r) for r in card.results],
        "not_produced": [{"email": n.key, "reason": n.reason} for n in card.not_produced],
        "recommendations": [
            {"job": rec.job, "choice": rec.choice, "reason": rec.reason}
            for rec in card.recommendations
        ],
    }


def _questions_json(card: QuestionsCard) -> dict[str, Any]:
    questions = card.questions
    return {
        "today": questions.today.isoformat(),
        "questions": len(questions.cases),
        "not_answerable": sum(not c.answerable for c in questions.cases),
        "results": [_result_json(r) for r in card.results],
    }


def to_json(scored: "Scorecard | Report") -> dict[str, Any]:
    report = _report(scored)
    named: list[tuple[str, CandidateResult]] = [
        (f"{card.prefix}{r.job}.{r.name}", r) for card in report.cards for r in card.results
    ]
    if report.questions:
        named += [(f"{r.job}.{r.name}", r) for r in report.questions.results]
    data: dict[str, Any] = {
        "run_date": report.run_date.isoformat(),
        "paid_this_run_usd": {k: round(v, 6) for k, v in report.paid_this_run.items()},
        "sets": {card.golden_set: _card_json(card) for card in report.cards},
    }
    if report.questions:
        data["questions"] = _questions_json(report.questions)
    data |= {
        "candidates": [name for name, _ in named],
        "not_run": {name: r.status for name, r in named if not r.ran},
        "scores": report.scores,
    }
    return data


def _golden_counts(card: Scorecard) -> dict[str, int]:
    billing = [c for c in card.cases if c.is_billing_document]
    return {
        "emails": len(one_per_email(card.cases)),
        "billing_documents": len(billing),
        "documents_extracted": len(billing) - len(card.not_produced),
        "on_expected_list": sum(c.vendor in card.expected_vendors for c in billing),
    }


# --- Markdown ------------------------------------------------------------------------------------


def _cell(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _row(cells: Sequence[object]) -> str:
    return "| " + " | ".join(_cell(c) for c in cells) + " |"


def _table(header: Sequence[object], rows: Sequence[Sequence[object]]) -> list[str]:
    return [_row(header), _row(["---"] * len(header)), *(_row(r) for r in rows), ""]


def _rate(rate: Rate | None) -> str:
    if rate is None or rate.share is None:
        return "-"
    return f"{rate.right}/{rate.total} ({rate.share:.1%})"


def _seconds(value: float | None) -> str:
    if value is None:
        return "-"
    # Rules answer in milliseconds that vary from run to run; the scorecard should not.
    return "under 0.1 s" if value < 0.1 else f"{value:.2f} s"


def _summary(job: str, results: Sequence[CandidateResult]) -> list[str]:
    def row(title: str, value: Callable[[CandidateResult], str]) -> list[str]:
        return [title, *(value(r) if r.ran else "-" for r in results)]

    rows: list[list[str]] = [
        ["Model", *(r.model for r in results)],
        ["Status", *(r.status for r in results)],
    ]
    rows += [row(METRIC_TITLES[m], lambda r, m=m: _rate(r.metrics.get(m))) for m in METRICS[job]]
    if job != QUESTIONS:
        rows.append(
            row(
                "Calibration error",
                lambda r: "-" if r.calibration_error is None else f"{r.calibration_error:.3f}",
            )
        )

    def cost(r: CandidateResult, show: Callable[[Any], str]) -> str:
        return "-" if r.cost is None else show(r.cost)

    rows += [
        row("Calls (failed)", lambda r: cost(r, lambda c: f"{c.calls} ({c.failed})")),
        row(
            "Input / output tokens",
            lambda r: cost(r, lambda c: f"{c.input_tokens:,} / {c.output_tokens:,}"),
        ),
        row("Cost of all answers", lambda r: cost(r, lambda c: f"${c.cost_usd:.4f}")),
        row(
            "Time per call, median / p95",
            lambda r: cost(
                r, lambda c: f"{_seconds(c.median_seconds)} / {_seconds(c.p95_seconds)}"
            ),
        ),
    ]
    return _table(["Measure", *(r.name for r in results)], rows)


def _calibration(result: CandidateResult, right_means: str) -> list[str]:
    lines: list[str] = []
    for title, groups in (
        ("By stated confidence", result.by_confidence),
        ("By stated probability", result.by_probability),
    ):
        if not groups:
            continue
        lines += [f"{title}, {result.name} (right means {right_means}):", ""]
        lines += _table(
            ["Stated", "Answers", "Right", "Share right", "Probability stated"],
            [
                [g.group, g.count, g.right, f"{g.right / g.count:.1%}", f"{g.stated:.2f}"]
                for g in groups
            ],
        )
    return lines


def _confusion(result: CandidateResult) -> list[str]:
    answered = sorted({a for row in result.confusion.values() for a in row})
    rows = [
        [expected, *(result.confusion[expected].get(a, 0) or "" for a in answered)]
        for expected in sorted(result.confusion)
    ]
    return [
        f"Confusion, {result.name} (rows: golden kind; columns: answered kind):",
        "",
        *_table(["Golden \\ answered", *answered], rows),
    ]


def _breakdown(
    dimension: str,
    results: Sequence[CandidateResult],
    metric: str = ALL_FIELDS_RIGHT,
    counted: str = "Documents",
    right_means: str = "all fields right",
) -> list[str]:
    ran = [r for r in results if r.ran]
    groups = sorted({g for r in ran for g in r.breakdowns.get(dimension, {})})
    if not groups:
        return []
    rows: list[list[str]] = []
    for group in groups:
        counts = [r.breakdowns[dimension].get(group, {}).get(metric) for r in ran]
        total = next((c.total for c in counts if c is not None), 0)
        rows.append([group, str(total), *(_rate(c) for c in counts)])
    return [
        f"By {dimension} ({right_means}):",
        "",
        *_table([dimension.capitalize(), counted, *(r.name for r in ran)], rows),
    ]


def _demoted(lines: Sequence[str]) -> list[str]:
    """The lines one heading level down, so a section can sit inside another."""
    return [f"#{line}" if line.startswith("#") else line for line in lines]


def _card_markdown(card: Scorecard) -> list[str]:
    counts = _golden_counts(card)
    lines = [
        f"# {SET_TITLES.get(card.golden_set, card.golden_set)}",
        "",
        f"{counts['emails']} emails from {SET_SOURCES.get(card.golden_set, card.golden_set)}, "
        f"with {counts['billing_documents']} billing documents, of which "
        f"{counts['documents_extracted']} produce a PDF to extract and "
        f"{counts['on_expected_list']} are from an expected vendor.",
        "",
        "## Summary",
        "",
    ]
    descriptions = {
        CLASSIFICATION: f"{counts['emails']} emails",
        EXTRACTION: f"{counts['documents_extracted']} documents",
        MATCHING: f"{counts['billing_documents']} billing documents, "
        f"{counts['billing_documents'] - counts['on_expected_list']} of them off the list",
    }
    for job in card.jobs:
        lines += [f"### {JOB_TITLES[job]} ({descriptions[job]})", "", *_summary(job, card.of(job))]

    if card.recommendations:
        lines += ["## Recommendation for ADR 0009", ""]
        lines += [
            "The rule: for each job, Jev becomes the default only if it matches Claude Haiku on "
            "accuracy and is better calibrated.",
            "",
        ]
        lines += [
            f"- {JOB_TITLES[rec.job]}: **{rec.choice}**. {rec.reason}"
            for rec in card.recommendations
        ]
        lines.append("")

    if CLASSIFICATION in card.jobs:
        lines += ["## Classification", ""]
        results = card.of(CLASSIFICATION)
        lines += _breakdown(LABEL, results, ACCURACY, "Emails", "the kind was right")
        for r in results:
            if r.ran:
                lines += _confusion(r)
                lines += _calibration(r, "the kind was right")
    if EXTRACTION in card.jobs:
        lines += ["## Extraction", ""]
        results = card.of(EXTRACTION)
        for dimension in ("invoice format", "vendor", LABEL):
            lines += _breakdown(dimension, results)
        for r in results:
            if r.ran:
                lines += _calibration(r, "all fields were right")
        if card.not_produced:
            lines += ["Billing documents with no PDF to extract:", ""]
            lines += _table(["Email", "Reason"], [[n.key, n.reason] for n in card.not_produced])
    if MATCHING in card.jobs:
        lines += ["## Vendor matching", ""]
        lines += [
            "Each candidate is given the document's text (the email's text when there is no PDF) "
            f"and the {len(card.expected_vendors)} expected vendors. The right answer is the "
            "golden vendor when it is on the list, and none of these when it is not.",
            "",
        ]
        results = card.of(MATCHING)
        lines += _breakdown(LABEL, results, ACCURACY, "Billing documents", "the match was right")
        for r in results:
            if r.ran:
                lines += _calibration(r, "the match was right")

    lines += ["## Failures", ""]
    for job in card.jobs:
        failures = [f for r in card.of(job) for f in r.failures]
        lines += [f"### {JOB_TITLES[job]} ({len(failures)})", ""]
        if not failures:
            lines += ["None.", ""]
            continue
        lines += _table(
            ["Candidate", "Email", "Field", "Expected", "Returned", "Labels"],
            [
                [f.candidate, f.email, f.field, f.expected, f.returned, ", ".join(f.labels)]
                for f in failures
            ],
        )
    return lines


def _questions_markdown(card: QuestionsCard) -> list[str]:
    questions = card.questions
    declined = sum(not c.answerable for c in questions.cases)
    lines = [
        f"# {JOB_TITLES[QUESTIONS]}",
        "",
        f"{len(questions.cases)} questions in plain words, from `backend/evals/questions.json`, "
        f"of which {declined} should be declined. Today is fixed at "
        f"{questions.today.isoformat()}, and the model is told the ledger holds "
        f"{len(questions.vendors)} vendors, {len(questions.source_accounts)} source accounts and "
        f"the collection months {questions.months[0]} to {questions.months[-1]}. An answer is "
        "right when the query the server would run, and its parameters after the server's "
        "checks, are exactly the right ones; a question to decline is right when it is declined.",
        "",
        "## Summary",
        "",
        *_summary(QUESTIONS, card.results),
        *_breakdown(KIND, card.results, ACCURACY, "Questions", "query and parameters right"),
    ]
    failures = [f for r in card.results for f in r.failures]
    lines += [f"## Failures ({len(failures)})", ""]
    if not failures:
        return [*lines, "None.", ""]
    return lines + _table(
        ["Candidate", "Question", "Expected", "Returned", "Kinds"],
        [[f.candidate, f.email, f.expected, f.returned, ", ".join(f.labels)] for f in failures],
    )


def to_markdown(scored: "Scorecard | Report") -> str:
    report = _report(scored)
    paid = ", ".join(f"{p} ${usd:.4f}" for p, usd in report.paid_this_run.items()) or "nothing"
    lines = [
        "# Offline eval scorecard",
        "",
        f"Run on {report.run_date.isoformat()}. Paid for calls not in the cache: {paid}.",
        "",
        "Generated by `invoice-collector-eval`. Each golden set is scored on its own, and the "
        "questions eval after them. Costs are what every answer cost when first produced, and "
        "cached answers keep the tokens and time of the call that produced them.",
        "",
        "Calibration error is the gap between the probability an answer stated and the share of "
        "such answers that were right, weighted by the number of answers. A candidate that states "
        "only a label is taken to mean high 0.95, medium 0.80, low 0.35 (the middle of the bands "
        "the project gives those labels). Lower is better.",
        "",
    ]
    for card in report.cards:
        lines += _demoted(_card_markdown(card))
    if report.questions:
        lines += _demoted(_questions_markdown(report.questions))
    return "\n".join(lines).rstrip() + "\n"
