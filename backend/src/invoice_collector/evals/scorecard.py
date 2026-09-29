"""The scorecard: the same results as Markdown for people and as JSON for the gate."""

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from invoice_collector.evals.documents import NotProduced
from invoice_collector.evals.golden import GoldenCase
from invoice_collector.evals.recommend import Recommendation, adr_0009
from invoice_collector.evals.scoring import (
    ACCURACY,
    ALL_FIELDS_RIGHT,
    BILLING_PRECISION,
    BILLING_RECALL,
    CLASSIFICATION,
    EXTRACTION,
    FIELDS,
    MATCHING,
    OFF_LIST,
    ON_LIST,
    CalibrationGroup,
    CandidateResult,
    Failure,
    Rate,
)

JOBS = (CLASSIFICATION, EXTRACTION, MATCHING)
JOB_TITLES = {
    CLASSIFICATION: "Classification",
    EXTRACTION: "Extraction",
    MATCHING: "Vendor matching",
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
}
METRICS = {
    CLASSIFICATION: (ACCURACY, BILLING_PRECISION, BILLING_RECALL),
    EXTRACTION: (*FIELDS, ALL_FIELDS_RIGHT),
    MATCHING: (ACCURACY, ON_LIST, OFF_LIST),
}


@dataclass(frozen=True)
class Scorecard:
    run_date: date
    jobs: tuple[str, ...]
    cases: Sequence[GoldenCase]
    results: Sequence[CandidateResult]
    not_produced: Sequence[NotProduced]
    expected_vendors: Sequence[str]

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
        return {
            f"{r.job}.{r.name}.{metric}": share
            for r in self.results
            if r.ran
            for metric, rate in r.metrics.items()
            if (share := rate.share) is not None
        }

    @property
    def paid_this_run(self) -> dict[str, float]:
        paid: dict[str, float] = {}
        for r in self.results:
            if r.cost is not None and r.provider != "none":
                paid[r.provider] = paid.get(r.provider, 0.0) + r.cost.paid_usd
        return dict(sorted(paid.items()))

    def write(self, folder: Path) -> tuple[Path, Path]:
        folder.mkdir(parents=True, exist_ok=True)
        markdown, data = folder / "scorecard.md", folder / "scorecard.json"
        markdown.write_text(to_markdown(self), "utf-8")
        data.write_text(json.dumps(to_json(self), indent=2) + "\n", "utf-8")
        return markdown, data


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


def to_json(card: Scorecard) -> dict[str, Any]:
    return {
        "run_date": card.run_date.isoformat(),
        "paid_this_run_usd": {k: round(v, 6) for k, v in card.paid_this_run.items()},
        "golden": _golden_counts(card),
        "results": [_result_json(r) for r in card.results],
        "not_produced": [{"email": n.key, "reason": n.reason} for n in card.not_produced],
        "recommendations": [
            {"job": rec.job, "choice": rec.choice, "reason": rec.reason}
            for rec in card.recommendations
        ],
        "candidates": [f"{r.job}.{r.name}" for r in card.results],
        "not_run": {f"{r.job}.{r.name}": r.status for r in card.results if not r.ran},
        "scores": card.scores,
    }


def _golden_counts(card: Scorecard) -> dict[str, int]:
    billing = [c for c in card.cases if c.is_billing_document]
    return {
        "emails": len(card.cases),
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


def _breakdown(dimension: str, results: Sequence[CandidateResult]) -> list[str]:
    ran = [r for r in results if r.ran]
    groups = sorted({g for r in ran for g in r.breakdowns.get(dimension, {})})
    rows: list[list[str]] = []
    for group in groups:
        counts = [r.breakdowns[dimension].get(group, {}).get(ALL_FIELDS_RIGHT) for r in ran]
        total = next((c.total for c in counts if c is not None), 0)
        rows.append([group, str(total), *(_rate(c) for c in counts)])
    return [
        f"By {dimension} (all fields right):",
        "",
        *_table([dimension.capitalize(), "Documents", *(r.name for r in ran)], rows),
    ]


def to_markdown(card: Scorecard) -> str:
    counts = _golden_counts(card)
    paid = ", ".join(f"{p} ${usd:.4f}" for p, usd in card.paid_this_run.items()) or "nothing"
    lines = [
        "# Offline eval scorecard",
        "",
        f"Run on {card.run_date.isoformat()}. Paid for calls not in the cache: {paid}.",
        "",
        f"Golden dataset: {counts['emails']} emails, {counts['billing_documents']} billing "
        f"documents, of which {counts['documents_extracted']} produce a PDF to extract and "
        f"{counts['on_expected_list']} are from an expected vendor. Generated by "
        "`invoice-collector-eval`; costs are what every answer cost when first produced, and "
        "cached answers keep the tokens and time of the call that produced them.",
        "",
        "Calibration error is the gap between the probability an answer stated and the share of "
        "such answers that were right, weighted by the number of answers. A candidate that states "
        "only a label is taken to mean high 0.95, medium 0.80, low 0.35 (the middle of the bands "
        "the project gives those labels). Lower is better.",
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
        for r in card.of(CLASSIFICATION):
            if r.ran:
                lines += _confusion(r)
                lines += _calibration(r, "the kind was right")
    if EXTRACTION in card.jobs:
        lines += ["## Extraction", ""]
        results = card.of(EXTRACTION)
        for dimension in ("invoice format", "vendor", "hard-case label"):
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
        for r in card.of(MATCHING):
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
    return "\n".join(lines).rstrip() + "\n"
