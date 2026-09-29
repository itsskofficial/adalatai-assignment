"""Command line entry point."""

import argparse
import os
import sys
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import anthropic
from dotenv import find_dotenv, load_dotenv
from google.oauth2.credentials import Credentials

from invoice_collector import drive_archive, google_auth
from invoice_collector.archive import Archive, BothArchives, LocalArchive
from invoice_collector.browser import HeadlessBrowser
from invoice_collector.classifier import Classifier, FallbackClassifier, RuleClassifier
from invoice_collector.claude_classifier import ClaudeClassifier
from invoice_collector.claude_extractor import DEFAULT_MODEL, ClaudeExtractor
from invoice_collector.destinations import DestinationPolicy
from invoice_collector.digest import (
    DigestNotSent,
    DigestSender,
    SlackWebhook,
    build_digest,
    render,
    render_failure,
)
from invoice_collector.domain import CollectionMonth
from invoice_collector.drive_archive import DriveArchive
from invoice_collector.exchange_rates import FrankfurterExchangeRates, NoExchangeRates
from invoice_collector.extractor import Extractor, FallbackExtractor
from invoice_collector.gap_report import lines as gap_lines
from invoice_collector.gap_report import read_expected_vendors, write_gaps
from invoice_collector.jev_classifier import JevClassifier
from invoice_collector.ledger import Ledger
from invoice_collector.rule_extractor import RuleExtractor
from invoice_collector.run import Pipeline, Settings, collect, seed_expected_vendors
from invoice_collector.samples import load_extractor, load_sources
from invoice_collector.sheet_summary import SheetSummary, month_report, spreadsheet_name
from invoice_collector.summary import CsvSummary

# Used by the rule extractor until the expected vendor list exists.
KNOWN_VENDORS = ("Slack", "Notion", "Figma", "Zoom", "Linear", "GitHub", "AWS", "Google Workspace")

# Drive and Sheets clients acting as the owner account.
GoogleServices = Callable[[Credentials], tuple[Any, Any]]
SLACK_WEBHOOK_VARIABLE = "INVOICE_COLLECTOR_SLACK_WEBHOOK"
DASHBOARD_URL_VARIABLE = "INVOICE_COLLECTOR_DASHBOARD_URL"


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
        choices=("jev", "claude", "rules"),
        default=None,
        help="how emails are classified (default: jev when JEV_API_KEY is set, otherwise "
        "claude when ANTHROPIC_API_KEY is set, otherwise rules)",
    )
    collect_cmd.add_argument(
        "--google-owner",
        metavar="ADDRESS",
        help="the owner account: also archive PDFs to its Google Drive and write the summary "
        "to a Google Sheet there. It must be signed in with invoice-collector-setup --owner",
    )
    collect_cmd.add_argument(
        "--token-dir",
        type=Path,
        default=google_auth.DEFAULT_TOKEN_DIR,
        help="where stored Google sign-ins are kept",
    )
    collect_cmd.add_argument(
        "--no-digest",
        action="store_true",
        help=f"do not send the digest to Slack, even when {SLACK_WEBHOOK_VARIABLE} is set",
    )
    return parser


def _owner_sign_in(owner: str, token_dir: Path) -> Credentials | None:
    """The owner account's stored sign-in for Drive, or None after saying how to sign in."""
    try:
        return google_auth.sign_in(owner, drive_archive.SCOPES, token_dir, allow_browser=False)
    except google_auth.SignInExpired:
        print(
            f"The owner account {owner} is not signed in to Google Drive, or its sign-in "
            f"no longer works. Nothing was collected.\n"
            f"Sign it in with: invoice-collector-setup --owner {owner}",
            file=sys.stderr,
        )
        return None


def _extractor(choice: str | None, samples: Path) -> Extractor:
    if choice is None:
        choice = "claude" if os.environ.get("ANTHROPIC_API_KEY") else "prepared"
    if choice == "prepared":
        return load_extractor(samples)
    model = os.environ.get("INVOICE_COLLECTOR_EXTRACTION_MODEL", DEFAULT_MODEL)
    return FallbackExtractor(
        ClaudeExtractor(anthropic.Anthropic(), model), RuleExtractor(KNOWN_VENDORS)
    )


def classifier_for(choice: str | None, environ: Mapping[str, str]) -> FallbackClassifier:
    """The classifier, with those it falls back to when it cannot answer.

    Jev is the default when its key is present: see ADR 0009. Whatever is chosen, the
    classifiers after it in the order Jev, Claude, rules stand behind it.
    """
    available = ["rules"]
    if environ.get("ANTHROPIC_API_KEY"):
        available.insert(0, "claude")
    if environ.get("JEV_API_KEY"):
        available.insert(0, "jev")
    if choice is None:
        choice = available[0]
    if choice not in available:
        raise SystemExit(f"the {choice} classifier needs its API key in the environment")

    model = environ.get("INVOICE_COLLECTOR_CLASSIFICATION_MODEL", DEFAULT_MODEL)
    chain: list[Classifier] = []
    for name in available[available.index(choice) :]:
        if name == "jev":
            chain.append(JevClassifier(environ["JEV_API_KEY"]))
        elif name == "claude":
            chain.append(ClaudeClassifier(anthropic.Anthropic(), model))
        else:
            chain.append(RuleClassifier())
    return FallbackClassifier(*chain)


def _digest_sender(
    no_digest: bool, sender_for: Callable[[str], DigestSender]
) -> DigestSender | None:
    """The sender for the digest, or None when no webhook is set or it is not usable."""
    url = os.environ.get(SLACK_WEBHOOK_VARIABLE)
    if no_digest or not url:
        return None
    try:
        return sender_for(url)
    except ValueError as refused:
        # The address is a secret, so only the reason is printed.
        print(f"Warning: digest not sent: {refused}")
        return None


def _send_digest(sender: DigestSender | None, message: dict[str, Any]) -> None:
    if sender is None:
        return
    try:
        sender.send(message)
    except DigestNotSent as not_sent:
        print(f"Warning: digest not sent: {not_sent}")


def main(
    argv: Sequence[str] | None = None,
    google_services: GoogleServices = google_auth.drive_and_sheets_services,
    *,
    digest_sender_for: Callable[[str], DigestSender] = SlackWebhook,
) -> int:
    if not os.environ.get("INVOICE_COLLECTOR_SKIP_DOTENV"):
        load_dotenv(find_dotenv(usecwd=True))
    args = _parser().parse_args(argv)
    month: CollectionMonth = args.month
    out: Path = args.out
    summary_path = out / f"{month}_summary.csv"
    digest_sender = _digest_sender(args.no_digest, digest_sender_for)

    archive: Archive = LocalArchive(out / "archive")
    drive: Any = None
    sheets: Any = None
    if args.google_owner:
        credentials = _owner_sign_in(args.google_owner, args.token_dir)
        if credentials is None:
            not_signed_in = f"the owner account {args.google_owner} is not signed in to Google"
            _send_digest(digest_sender, render_failure(month, not_signed_in))
            return 1
        try:
            drive, sheets = google_services(credentials)
        except Exception as error:
            _send_digest(digest_sender, render_failure(month, str(error) or type(error).__name__))
            raise
        # Drive comes first, so the summary links to each PDF in Drive.
        archive = BothArchives(DriveArchive(drive), archive)

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
                    classifier=classifier_for(args.classifier, os.environ),
                    extractor=_extractor(args.extractor, args.samples),
                    renderer=browser,
                    portal_fetcher=browser,
                    exchange_rates=(
                        NoExchangeRates() if args.no_exchange_rates else FrankfurterExchangeRates()
                    ),
                    archive=archive,
                    ledger=ledger,
                ),
                summary_writers=[CsvSummary(summary_path)],
                settings=Settings(search_window_days=args.search_window_days),
            )
        if sheets is not None:
            # Written after the run, as its other tabs show what the run recorded.
            report = month_report(ledger, month)
            SheetSummary(sheets, drive, month, report).write(result.summary)
        states = Counter(e.state.value for e in ledger.examined_emails(month))
        digest = build_digest(
            month,
            result,
            ledger,
            gaps=[(g.vendor, g.kind, g.explanation) for g in result.gaps],
            failed_source_accounts=sorted(result.failed_source_accounts),
            summary_link=str(summary_path),
            dashboard_url=os.environ.get(DASHBOARD_URL_VARIABLE),
        )
    except Exception as error:
        _send_digest(digest_sender, render_failure(month, str(error) or type(error).__name__))
        raise
    finally:
        ledger.close()

    print(f"Collection month {month}: {len(result.summary)} billing documents collected")
    for state, count in sorted(states.items()):
        print(f"  {state}: {count}")
    print(f"Summary: {summary_path}")
    if sheets is not None:
        print(f"Sheet: {spreadsheet_name(month)}, in Google Drive")
    write_gaps(out / f"{month}_gaps.csv", result.gaps)
    for line in gap_lines(result):
        print(line)
    for warning in result.warnings:
        print(f"Warning: {warning}")
    _send_digest(digest_sender, render(digest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
