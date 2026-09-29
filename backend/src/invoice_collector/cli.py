"""Command line entry point."""

import argparse
import os
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

import anthropic
from dotenv import find_dotenv, load_dotenv

from invoice_collector.archive import LocalArchive
from invoice_collector.browser import HeadlessBrowser
from invoice_collector.classifier import Classifier, FallbackClassifier, RuleClassifier
from invoice_collector.claude_classifier import ClaudeClassifier
from invoice_collector.claude_extractor import DEFAULT_MODEL, ClaudeExtractor
from invoice_collector.destinations import DestinationPolicy
from invoice_collector.domain import CollectionMonth
from invoice_collector.exchange_rates import FrankfurterExchangeRates, NoExchangeRates
from invoice_collector.extractor import Extractor, FallbackExtractor
from invoice_collector.gap_report import lines as gap_lines
from invoice_collector.gap_report import read_expected_vendors, write_gaps
from invoice_collector.ledger import Ledger
from invoice_collector.rule_extractor import RuleExtractor
from invoice_collector.run import Pipeline, Settings, collect, seed_expected_vendors
from invoice_collector.samples import load_extractor, load_sources
from invoice_collector.summary import CsvSummary

# Used by the rule extractor until the expected vendor list exists.
KNOWN_VENDORS = ("Slack", "Notion", "Figma", "Zoom", "Linear", "GitHub", "AWS", "Google Workspace")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="invoice-collector")
    commands = parser.add_subparsers(dest="command", required=True)

    collect_cmd = commands.add_parser("collect", help="collect billing documents for a month")
    collect_cmd.add_argument("month", type=CollectionMonth.parse, help="collection month, YYYY-MM")
    collect_cmd.add_argument(
        "--samples", type=Path, required=True, help="folder of sample emails to read"
    )
    collect_cmd.add_argument("--out", type=Path, default=Path("out"), help="where to write output")
    collect_cmd.add_argument(
        "--allow-local-portals",
        action="store_true",
        help="follow portal links to this machine and the local network, for sample portal "
        "pages only. Never use this with real mail",
    )
    collect_cmd.add_argument(
        "--extractor",
        choices=("claude", "prepared"),
        default=None,
        help="how fields are read: by Claude, or from the prepared answers beside the samples "
        "(default: claude when ANTHROPIC_API_KEY is set, otherwise prepared)",
    )
    collect_cmd.add_argument(
        "--expected-vendors",
        type=Path,
        default=None,
        help="file that fills the expected vendor list on first run "
        "(default: expected_vendors.json beside the sample emails)",
    )
    collect_cmd.add_argument(
        "--no-exchange-rates",
        action="store_true",
        help="do not look up exchange rates; rupee amounts are left empty",
    )
    collect_cmd.add_argument(
        "--search-window-days",
        type=int,
        default=Settings().search_window_days,
        help="how many days either side of the month to look for emails",
    )
    collect_cmd.add_argument(
        "--classifier",
        choices=("claude", "rules"),
        default=None,
        help="how emails are classified "
        "(default: claude when ANTHROPIC_API_KEY is set, otherwise rules)",
    )
    return parser


def _extractor(choice: str | None, samples: Path) -> Extractor:
    if choice is None:
        choice = "claude" if os.environ.get("ANTHROPIC_API_KEY") else "prepared"
    if choice == "prepared":
        return load_extractor(samples)
    model = os.environ.get("INVOICE_COLLECTOR_EXTRACTION_MODEL", DEFAULT_MODEL)
    return FallbackExtractor(
        ClaudeExtractor(anthropic.Anthropic(), model), RuleExtractor(KNOWN_VENDORS)
    )


def _classifier(choice: str | None) -> Classifier:
    if choice is None:
        choice = "claude" if os.environ.get("ANTHROPIC_API_KEY") else "rules"
    if choice == "rules":
        return RuleClassifier()
    model = os.environ.get("INVOICE_COLLECTOR_CLASSIFICATION_MODEL", DEFAULT_MODEL)
    return FallbackClassifier(ClaudeClassifier(anthropic.Anthropic(), model), RuleClassifier())


def main(argv: Sequence[str] | None = None) -> int:
    if not os.environ.get("INVOICE_COLLECTOR_SKIP_DOTENV"):
        load_dotenv(find_dotenv(usecwd=True))
    args = _parser().parse_args(argv)
    month: CollectionMonth = args.month
    out: Path = args.out
    summary_path = out / f"{month}_summary.csv"

    policy = (
        DestinationPolicy.for_local_pages() if args.allow_local_portals else DestinationPolicy()
    )
    expected_vendors: Path = args.expected_vendors or args.samples / "expected_vendors.json"
    ledger = Ledger(out / "ledger.sqlite")
    try:
        if expected_vendors.exists():
            seed_expected_vendors(ledger, read_expected_vendors(expected_vendors))
        with HeadlessBrowser(policy) as browser:
            result = collect(
                month,
                sources=load_sources(args.samples),
                pipeline=Pipeline(
                    classifier=_classifier(args.classifier),
                    extractor=_extractor(args.extractor, args.samples),
                    renderer=browser,
                    portal_fetcher=browser,
                    exchange_rates=(
                        NoExchangeRates() if args.no_exchange_rates else FrankfurterExchangeRates()
                    ),
                    archive=LocalArchive(out / "archive"),
                    ledger=ledger,
                ),
                summary_writers=[CsvSummary(summary_path)],
                settings=Settings(search_window_days=args.search_window_days),
            )
        states = Counter(e.state.value for e in ledger.examined_emails(month))
    finally:
        ledger.close()

    print(f"Collection month {month}: {len(result.summary)} billing documents collected")
    for state, count in sorted(states.items()):
        print(f"  {state}: {count}")
    print(f"Summary: {summary_path}")
    write_gaps(out / f"{month}_gaps.csv", result.gaps)
    for line in gap_lines(result):
        print(line)
    for warning in result.warnings:
        print(f"Warning: {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
