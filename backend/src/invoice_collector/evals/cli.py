"""Command line for the offline eval: run it, check a scorecard against the baseline, accept it.

invoice-collector-eval run [--eval JOB ...] [--set standard|hard ...]
    [--classifier NAME ...] [--extractor NAME ...] [--matcher NAME ...] [--asker NAME ...]
invoice-collector-eval check [--tolerance METRIC=VALUE ...]
invoice-collector-eval accept
"""

import argparse
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from dotenv import find_dotenv, load_dotenv

from invoice_collector import tracing
from invoice_collector.classifier import Classifier
from invoice_collector.evals import candidates as named
from invoice_collector.evals.candidates import Candidate, QueryChooser
from invoice_collector.evals.documents import Document, NotProduced, pdf_text, produce_documents
from invoice_collector.evals.evaluate import (
    Estimate,
    Settings,
    classification_items,
    classification_tokens,
    estimate,
    extraction_items,
    extraction_tokens,
    matching_items,
    matching_tokens,
    run_classification,
    run_extraction,
    run_matching,
    run_questions,
)
from invoice_collector.evals.gate import accept, check, read_json
from invoice_collector.evals.golden import (
    GOLDEN_SETS,
    HARD,
    STANDARD,
    GoldenCase,
    load_expected_vendors,
    load_golden,
)
from invoice_collector.evals.questions import (
    QUESTIONS,
    QuestionSet,
    load_questions,
    question_items,
    question_tokens,
)
from invoice_collector.evals.runner import AnswerCache
from invoice_collector.evals.scorecard import (
    GOLDEN_JOBS,
    JOB_TITLES,
    JOBS,
    SET_TITLES,
    QuestionsCard,
    Report,
    Scorecard,
)
from invoice_collector.evals.scoring import (
    CLASSIFICATION,
    EXTRACTION,
    MATCHING,
    CandidateResult,
)
from invoice_collector.extractor import Extractor
from invoice_collector.renderer import Renderer
from invoice_collector.vendor_matcher import VendorMatcher

BACKEND = Path(__file__).resolve().parents[3]
DEFAULT_SAMPLES = BACKEND / "samples"
DEFAULT_CACHE = BACKEND / ".eval-cache"
DEFAULT_OUT = BACKEND / "evals"
DEFAULT_HARD = DEFAULT_OUT / "hard"
DEFAULT_QUESTIONS = DEFAULT_OUT / "questions.json"

DEFAULT_CANDIDATES = {
    CLASSIFICATION: ("claude-haiku", "jev", "rules"),
    EXTRACTION: ("claude-haiku", "rules"),
    MATCHING: ("jev", "rules"),
    QUESTIONS: ("claude-haiku",),
}
DEFAULT_BUDGET_USD = 1.0
# How long the eval waits for its experiments to reach Langfuse before it goes on.
EXPERIMENT_FLUSH_SECONDS = 30.0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="invoice-collector-eval")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="run the evals and write the scorecard")
    run.add_argument(
        "--eval",
        dest="evals",
        action="append",
        choices=JOBS,
        help="which eval to run; repeat for several (default: all four)",
    )
    run.add_argument(
        "--set",
        dest="sets",
        action="append",
        choices=GOLDEN_SETS,
        help="the golden set classification, extraction and matching run on; "
        "repeat for both (default: both)",
    )
    run.add_argument("--classifier", action="append", choices=named.CLASSIFIERS)
    run.add_argument("--extractor", action="append", choices=named.EXTRACTORS)
    run.add_argument("--matcher", action="append", choices=named.MATCHERS)
    run.add_argument("--asker", action="append", choices=named.ASKERS)
    run.add_argument("--no-cache", action="store_true", help="ask again; answers are still stored")
    run.add_argument("--concurrency", type=int, default=4, help="calls at a time (default 4)")
    run.add_argument(
        "--budget-usd",
        type=float,
        default=DEFAULT_BUDGET_USD,
        help="refuse to run when the estimated cost for any one provider is above this",
    )
    run.add_argument(
        "--estimate-only", action="store_true", help="print the estimated cost and stop"
    )
    run.add_argument("--env-file", type=Path, help="read API keys from this .env file")
    run.add_argument("--samples", type=Path, default=DEFAULT_SAMPLES, help="the standard set")
    run.add_argument("--hard-samples", type=Path, default=DEFAULT_HARD, help="the hard set")
    run.add_argument(
        "--questions", type=Path, default=DEFAULT_QUESTIONS, help="the questions eval's dataset"
    )
    run.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    run.add_argument("--out", type=Path, default=DEFAULT_OUT, help="where the scorecard goes")

    for name, text in (
        ("check", "fail when a score fell below the baseline"),
        ("accept", "make the scorecard's scores the new baseline"),
    ):
        sub = commands.add_parser(name, help=text)
        sub.add_argument("--scorecard", type=Path, default=DEFAULT_OUT / "scorecard.json")
        sub.add_argument("--baseline", type=Path, default=DEFAULT_OUT / "baseline.json")
        if name == "check":
            sub.add_argument(
                "--tolerance",
                action="append",
                default=[],
                metavar="METRIC=VALUE",
                help="how far a metric may fall, by name (e.g. accuracy=0.02) or full path",
            )
    return parser


def _tolerances(pairs: Sequence[str]) -> dict[str, float]:
    tolerances: dict[str, float] = {}
    for pair in pairs:
        metric, _, value = pair.partition("=")
        tolerances[metric.strip()] = float(value)
    return tolerances


def _check(args: argparse.Namespace) -> int:
    result = check(read_json(args.scorecard), read_json(args.baseline), _tolerances(args.tolerance))
    for note in result.notes:
        print(f"note: {note}")
    for fall in result.falls:
        print(f"FELL: {fall}")
    print("The eval passed the gate." if result.passed else "The eval failed the gate.")
    return 0 if result.passed else 1


def _accept(args: argparse.Namespace) -> int:
    accept(read_json(args.scorecard), args.baseline)
    print(f"Wrote {args.baseline}")
    return 0


@dataclass(frozen=True)
class _GoldenSet:
    """One golden set as a run uses it: its cases, expected vendors and documents."""

    name: str
    cases: list[GoldenCase]
    expected_vendors: tuple[str, ...]
    documents: list[Document]
    not_produced: list[NotProduced]


def _chosen(job: str, given: list[str] | None) -> tuple[str, ...]:
    return tuple(dict.fromkeys(given)) if given else DEFAULT_CANDIDATES[job]


class _Plan:
    """The candidates and items of one run, prepared before any call is made."""

    def __init__(self, args: argparse.Namespace, renderer: Renderer) -> None:
        self.jobs: tuple[str, ...] = tuple(j for j in JOBS if j in (args.evals or JOBS))
        self.golden_jobs: tuple[str, ...] = tuple(j for j in self.jobs if j in GOLDEN_JOBS)
        env = dict(os.environ)

        folders: dict[str, Path] = {STANDARD: args.samples, HARD: args.hard_samples}
        names = tuple(dict.fromkeys(args.sets or GOLDEN_SETS)) if self.golden_jobs else ()
        self.sets: list[_GoldenSet] = []
        for name in names:
            folder = folders[name]
            cases = load_golden(folder)
            documents: list[Document] = []
            not_produced: list[NotProduced] = []
            if EXTRACTION in self.jobs or MATCHING in self.jobs:
                documents, not_produced = produce_documents(cases, renderer, folder / "portal")
            self.sets.append(
                _GoldenSet(name, cases, load_expected_vendors(folder), documents, not_produced)
            )

        # The rule extractor looks for the names of the expected vendors, the same in both sets.
        known = self.sets[0].expected_vendors if self.sets else ()
        self.classifiers: list[Candidate[Classifier]] = [
            named.classifier_candidate(n, env) for n in _chosen(CLASSIFICATION, args.classifier)
        ]
        self.extractors: list[Candidate[Extractor]] = [
            named.extractor_candidate(n, env, known) for n in _chosen(EXTRACTION, args.extractor)
        ]
        self.matchers: list[Candidate[VendorMatcher]] = [
            named.matcher_candidate(n, env) for n in _chosen(MATCHING, args.matcher)
        ]
        self.questions: QuestionSet | None = None
        self.askers: Sequence[Candidate[QueryChooser]] = ()
        if QUESTIONS in self.jobs:
            self.questions = load_questions(args.questions)
            today = self.questions.today
            self.askers = [
                named.asker_candidate(n, env, today) for n in _chosen(QUESTIONS, args.asker)
            ]

    def estimates(self, settings: Settings) -> list[tuple[str, str, Estimate]]:
        """Each candidate's estimate, under a title naming the set and the job."""
        found: list[tuple[str, str, Estimate]] = []
        for golden in self.sets:

            def title(job: str, golden: _GoldenSet = golden) -> str:
                return f"{SET_TITLES[golden.name]}, {JOB_TITLES[job]}"

            if CLASSIFICATION in self.jobs:
                items = classification_items(golden.cases)
                for c in self.classifiers:
                    found.append(
                        (
                            title(CLASSIFICATION),
                            c.name,
                            estimate(c, items, classification_tokens, settings),
                        )
                    )
            if EXTRACTION in self.jobs:
                pages = {doc.pdf: doc.pages for doc in golden.documents}
                tokens = extraction_tokens(
                    lambda pdf, pages=pages: pages.get(pdf) or pdf_text(pdf)[1]
                )
                ext_items = extraction_items(golden.documents)
                for c in self.extractors:
                    found.append(
                        (title(EXTRACTION), c.name, estimate(c, ext_items, tokens, settings))
                    )
            if MATCHING in self.jobs:
                m_items = matching_items(golden.cases, golden.documents, golden.expected_vendors)
                for c in self.matchers:
                    found.append(
                        (title(MATCHING), c.name, estimate(c, m_items, matching_tokens, settings))
                    )
        if self.questions is not None:
            q_items = question_items(self.questions)
            for c in self.askers:
                found.append(
                    (JOB_TITLES[QUESTIONS], c.name, estimate(c, q_items, question_tokens, settings))
                )
        return found

    def run(self, settings: Settings, run_date: date) -> Report:
        cards: list[Scorecard] = []
        for golden in self.sets:
            results: list[CandidateResult] = []
            on_set = replace(settings, golden_set=golden.name)
            if CLASSIFICATION in self.jobs:
                results += run_classification(self.classifiers, golden.cases, on_set)
            if EXTRACTION in self.jobs:
                results += run_extraction(self.extractors, golden.cases, golden.documents, on_set)
            if MATCHING in self.jobs:
                results += run_matching(
                    self.matchers, golden.cases, golden.documents, golden.expected_vendors, on_set
                )
            cards.append(
                Scorecard(
                    run_date=run_date,
                    jobs=self.golden_jobs,
                    cases=golden.cases,
                    results=results,
                    not_produced=golden.not_produced,
                    expected_vendors=golden.expected_vendors,
                    golden_set=golden.name,
                )
            )
        questions = None
        if self.questions is not None:
            asked = run_questions(self.askers, self.questions, settings)
            questions = QuestionsCard(self.questions, asked)
        return Report(run_date, tuple(cards), questions)


def _print_estimates(estimates: Sequence[tuple[str, str, Estimate]]) -> dict[str, float]:
    by_provider: dict[str, float] = {}
    print("Estimated cost of the calls not already in the cache:")
    for title, name, found in estimates:
        if found.calls:
            print(f"  {title}, {name}: {found.calls} calls, about ${found.usd:.4f}")
        by_provider[found.provider] = by_provider.get(found.provider, 0.0) + found.usd
    for provider, usd in sorted(by_provider.items()):
        if provider != "none":
            print(f"  Total for {provider}: about ${usd:.4f}")
    return by_provider


def _run(args: argparse.Namespace, renderer: Renderer | None = None) -> int:
    if args.env_file:
        load_dotenv(args.env_file)
    elif not os.environ.get("INVOICE_COLLECTOR_SKIP_DOTENV"):
        load_dotenv(find_dotenv(usecwd=True))

    if renderer is None:
        from invoice_collector.browser import HeadlessBrowser

        with HeadlessBrowser() as browser:
            plan = _Plan(args, browser)
    else:
        plan = _Plan(args, renderer)

    # Each candidate's run is recorded as an experiment when Langfuse's keys are set.
    tracer = tracing.tracer_from_environment()
    settings = Settings(
        AnswerCache(args.cache_dir),
        not args.no_cache,
        args.concurrency,
        tracer=tracer,
        run_label=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    by_provider = _print_estimates(plan.estimates(settings))
    over = [p for p, usd in by_provider.items() if p != "none" and usd > args.budget_usd]
    if over:
        print(
            f"Refusing to run: the estimate for {', '.join(over)} is above the budget of "
            f"${args.budget_usd:.2f}. Choose fewer candidates or raise --budget-usd.",
            file=sys.stderr,
        )
        return 3
    if args.estimate_only:
        return 0

    report = plan.run(settings, date.today())
    unsent = tracing.flush(tracer, EXPERIMENT_FLUSH_SECONDS)
    if unsent is not None:
        print(f"Warning: {unsent}", file=sys.stderr)
    markdown, _ = report.write(args.out)
    _print_summary(report)
    print(f"Wrote {markdown} and scorecard.json")
    return 0


def _headline(r: CandidateResult) -> str:
    headline = ", ".join(
        f"{name} {rate.share:.3f}" for name, rate in r.metrics.items() if rate.share is not None
    )
    return f"{r.name}: {r.status}{'; ' + headline if headline else ''}"


def _print_summary(report: Report) -> None:
    for card in report.cards:
        title = SET_TITLES[card.golden_set]
        for r in card.results:
            print(f"{title}, {JOB_TITLES[r.job]}, {_headline(r)}")
        for rec in card.recommendations:
            print(f"{title}, ADR 0009, {JOB_TITLES[rec.job]}: {rec.choice}. {rec.reason}")
    if report.questions:
        for r in report.questions.results:
            print(f"{JOB_TITLES[QUESTIONS]}, {_headline(r)}")
    paid = report.paid_this_run
    if paid:
        print("Paid this run: " + ", ".join(f"{p} ${usd:.4f}" for p, usd in paid.items()))


def main(argv: Sequence[str] | None = None, renderer: Renderer | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0].startswith("-"):
        arguments.insert(0, "run")
    args = _parser().parse_args(arguments)
    command: Any = args.command
    if command == "check":
        return _check(args)
    if command == "accept":
        return _accept(args)
    return _run(args, renderer)


if __name__ == "__main__":
    raise SystemExit(main())
