"""Command line for the offline eval: run it, check a scorecard against the baseline, accept it.

invoice-collector-eval run [--classifier NAME ...] [--extractor NAME ...] [--matcher NAME ...]
invoice-collector-eval check [--tolerance METRIC=VALUE ...]
invoice-collector-eval accept
"""

import argparse
import os
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

from dotenv import find_dotenv, load_dotenv

from invoice_collector.classifier import Classifier
from invoice_collector.evals import candidates as named
from invoice_collector.evals.candidates import Candidate
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
)
from invoice_collector.evals.gate import accept, check, read_json
from invoice_collector.evals.golden import GoldenCase, load_expected_vendors, load_golden
from invoice_collector.evals.runner import AnswerCache
from invoice_collector.evals.scorecard import JOB_TITLES, JOBS, Scorecard
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

DEFAULT_CANDIDATES = {
    CLASSIFICATION: ("claude-haiku", "jev", "rules"),
    EXTRACTION: ("claude-haiku", "rules"),
    MATCHING: ("jev", "rules"),
}
DEFAULT_BUDGET_USD = 1.0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="invoice-collector-eval")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="run the evals and write the scorecard")
    run.add_argument(
        "--eval",
        dest="evals",
        action="append",
        choices=JOBS,
        help="which eval to run; repeat for several (default: all three)",
    )
    run.add_argument("--classifier", action="append", choices=named.CLASSIFIERS)
    run.add_argument("--extractor", action="append", choices=named.EXTRACTORS)
    run.add_argument("--matcher", action="append", choices=named.MATCHERS)
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
    run.add_argument("--samples", type=Path, default=DEFAULT_SAMPLES)
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


class _Plan:
    """The candidates and items of one run, prepared before any call is made."""

    def __init__(self, args: argparse.Namespace, renderer: Renderer) -> None:
        self.jobs: tuple[str, ...] = tuple(j for j in JOBS if j in (args.evals or JOBS))
        self.cases: list[GoldenCase] = load_golden(args.samples)
        self.expected_vendors = load_expected_vendors(args.samples)
        env = dict(os.environ)

        def chosen(job: str, given: list[str] | None) -> tuple[str, ...]:
            return tuple(dict.fromkeys(given)) if given else DEFAULT_CANDIDATES[job]

        self.classifiers: list[Candidate[Classifier]] = [
            named.classifier_candidate(n, env) for n in chosen(CLASSIFICATION, args.classifier)
        ]
        self.extractors: list[Candidate[Extractor]] = [
            named.extractor_candidate(n, env, self.expected_vendors)
            for n in chosen(EXTRACTION, args.extractor)
        ]
        self.matchers: list[Candidate[VendorMatcher]] = [
            named.matcher_candidate(n, env) for n in chosen(MATCHING, args.matcher)
        ]
        self.documents: list[Document] = []
        self.not_produced: list[NotProduced] = []
        if EXTRACTION in self.jobs or MATCHING in self.jobs:
            self.documents, self.not_produced = produce_documents(
                self.cases, renderer, args.samples / "portal"
            )

    def estimates(self, settings: Settings) -> list[tuple[str, str, Estimate]]:
        found: list[tuple[str, str, Estimate]] = []
        if CLASSIFICATION in self.jobs:
            items = classification_items(self.cases)
            for c in self.classifiers:
                found.append(
                    (CLASSIFICATION, c.name, estimate(c, items, classification_tokens, settings))
                )
        if EXTRACTION in self.jobs:
            pages = {doc.pdf: doc.pages for doc in self.documents}
            tokens = extraction_tokens(lambda pdf: pages.get(pdf) or pdf_text(pdf)[1])
            ext_items = extraction_items(self.documents)
            for c in self.extractors:
                found.append((EXTRACTION, c.name, estimate(c, ext_items, tokens, settings)))
        if MATCHING in self.jobs:
            m_items = matching_items(self.cases, self.documents, self.expected_vendors)
            for c in self.matchers:
                found.append((MATCHING, c.name, estimate(c, m_items, matching_tokens, settings)))
        return found

    def run(self, settings: Settings) -> list[CandidateResult]:
        results: list[CandidateResult] = []
        if CLASSIFICATION in self.jobs:
            results += run_classification(self.classifiers, self.cases, settings)
        if EXTRACTION in self.jobs:
            results += run_extraction(self.extractors, self.cases, self.documents, settings)
        if MATCHING in self.jobs:
            results += run_matching(
                self.matchers, self.cases, self.documents, self.expected_vendors, settings
            )
        return results


def _print_estimates(estimates: Sequence[tuple[str, str, Estimate]]) -> dict[str, float]:
    by_provider: dict[str, float] = {}
    print("Estimated cost of the calls not already in the cache:")
    for job, name, found in estimates:
        if found.calls:
            print(f"  {JOB_TITLES[job]}, {name}: {found.calls} calls, about ${found.usd:.4f}")
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

    settings = Settings(AnswerCache(args.cache_dir), not args.no_cache, args.concurrency)
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

    card = Scorecard(
        run_date=date.today(),
        jobs=plan.jobs,
        cases=plan.cases,
        results=plan.run(settings),
        not_produced=plan.not_produced,
        expected_vendors=plan.expected_vendors,
    )
    markdown, _ = card.write(args.out)
    _print_summary(card)
    print(f"Wrote {markdown} and scorecard.json")
    return 0


def _print_summary(card: Scorecard) -> None:
    for r in card.results:
        headline = ", ".join(
            f"{name} {rate.share:.3f}" for name, rate in r.metrics.items() if rate.share is not None
        )
        print(f"{JOB_TITLES[r.job]}, {r.name}: {r.status}{'; ' + headline if headline else ''}")
    paid = card.paid_this_run
    if paid:
        print("Paid this run: " + ", ".join(f"{p} ${usd:.4f}" for p, usd in paid.items()))
    for rec in card.recommendations:
        print(f"ADR 0009, {JOB_TITLES[rec.job]}: {rec.choice}. {rec.reason}")


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
