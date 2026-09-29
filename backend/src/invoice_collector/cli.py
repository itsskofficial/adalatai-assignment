"""Command line entry point."""

import argparse
import csv
import os
import sys
from collections import Counter
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any, Protocol

import anthropic
from dotenv import find_dotenv, load_dotenv
from google.oauth2.credentials import Credentials

from invoice_collector import drive_archive, google_auth, run_cost, tracing
from invoice_collector.archive import Archive, BothArchives, LocalArchive
from invoice_collector.browser import HeadlessBrowser
from invoice_collector.classifier import Classifier, FallbackClassifier, RuleClassifier
from invoice_collector.claude_classifier import ClaudeClassifier
from invoice_collector.claude_extractor import DEFAULT_MODEL, ClaudeExtractor
from invoice_collector.claude_vendor_matcher import DEFAULT_MODEL as CLAUDE_MATCHING_MODEL
from invoice_collector.claude_vendor_matcher import ClaudeVendorMatcher
from invoice_collector.destinations import DestinationPolicy
from invoice_collector.digest import (
    DigestNotSent,
    DigestSender,
    SlackWebhook,
    build_digest,
    render,
    render_failure,
)
from invoice_collector.domain import CollectionMonth, EmailState, StartedBy
from invoice_collector.drive_archive import DriveArchive
from invoice_collector.exchange_rates import FrankfurterExchangeRates, NoExchangeRates
from invoice_collector.extractor import Extractor, FallbackExtractor
from invoice_collector.gap_report import lines as gap_lines
from invoice_collector.gap_report import read_expected_vendors, write_gaps
from invoice_collector.gmail_source import GmailMailSource
from invoice_collector.jev_classifier import JevClassifier
from invoice_collector.ledger import Ledger
from invoice_collector.mail_source import MailSource
from invoice_collector.metering import NOT_METERED, Meter, RunMeter, describe_cost
from invoice_collector.portal import PortalFetcher
from invoice_collector.renderer import Renderer
from invoice_collector.rule_extractor import RuleExtractor
from invoice_collector.run import Pipeline, RunResult, Settings, collect, seed_expected_vendors
from invoice_collector.samples import load_extractor, load_sources
from invoice_collector.sheet_summary import (
    SheetSummary,
    month_report,
    skipped_and_failed,
    spreadsheet_name,
)
from invoice_collector.source_account_registry import connected_source_accounts
from invoice_collector.summary import CsvSummary, SummaryWriter
from invoice_collector.vendor_matcher import (
    JevVendorMatcher,
    RulesFirstVendorMatcher,
    VendorMatcher,
)

# Used by the rule extractor until the expected vendor list exists.
KNOWN_VENDORS = ("Slack", "Notion", "Figma", "Zoom", "Linear", "GitHub", "AWS", "Google Workspace")

# Drive and Sheets clients acting as the owner account.
GoogleServices = Callable[[Credentials], tuple[Any, Any]]
SLACK_WEBHOOK_VARIABLE = "INVOICE_COLLECTOR_SLACK_WEBHOOK"
DASHBOARD_URL_VARIABLE = "INVOICE_COLLECTOR_DASHBOARD_URL"
# The mail source of one source account, given the folder of stored sign-ins.
MailSourceFor = Callable[[str, Path], MailSource]
ClaudeClient = Callable[[], anthropic.Anthropic]


class Browser(Renderer, PortalFetcher, Protocol):
    """Renders email bodies and fetches portal pages."""


# Opens the browser a collection renders and fetches with.
BrowserFactory = Callable[[DestinationPolicy], AbstractContextManager[Browser]]


class Collector(Protocol):
    """Collects a month, as invoice_collector.run.collect does."""

    def __call__(
        self,
        month: CollectionMonth,
        *,
        sources: Sequence[MailSource],
        pipeline: Pipeline,
        summary_writers: Sequence[SummaryWriter],
        settings: Settings | None = None,
        started_by: StartedBy = "command_line",
    ) -> RunResult: ...


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="invoice-collector")
    commands = parser.add_subparsers(dest="command", required=True)

    collect_cmd = commands.add_parser("collect", help="collect billing documents for a month")
    collect_cmd.add_argument("month", type=CollectionMonth.parse, help="collection month, YYYY-MM")
    add_collection_options(collect_cmd)

    cost_cmd = commands.add_parser(
        "cost",
        help="the measured cost of a run's model calls, per step and per billing document, "
        "read from their traces in Langfuse",
    )
    cost_cmd.add_argument("month", type=CollectionMonth.parse, help="collection month, YYYY-MM")
    cost_cmd.add_argument("--out", type=Path, default=Path("out"), help="where the run wrote")
    cost_cmd.add_argument(
        "--run", type=int, default=None, help="the run's number (default: the month's latest)"
    )
    return parser


def add_collection_options(collect_cmd: argparse.ArgumentParser) -> None:
    """The options of the collect command, apart from the collection month."""
    collect_cmd.add_argument("--samples", type=Path, help="folder of sample emails to read")
    collect_cmd.add_argument(
        "--account",
        action="append",
        default=[],
        metavar="ADDRESS",
        help="a source account to read through Gmail, signed in with invoice-collector-setup; "
        "repeat for each source account. Use this or --samples",
    )
    collect_cmd.add_argument(
        "--connected-accounts",
        action="store_true",
        help="read every source account connected in the dashboard through Gmail, "
        "instead of naming each with --account",
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
        "(default with --samples: expected_vendors.json beside the sample emails)",
    )
    collect_cmd.add_argument(
        "--map",
        action="append",
        default=[],
        metavar="SAMPLE=REAL",
        help="a sample source account named in the expected vendor file and the real address "
        "that receives its emails, as given to invoice-collector-seed gmail; repeat for each",
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
        "--vendor-matcher",
        choices=("jev", "claude", "rules"),
        default=None,
        help="what matches a vendor to the expected vendor list when rules cannot decide "
        "(default: jev when JEV_API_KEY is set, otherwise claude when ANTHROPIC_API_KEY is "
        "set, otherwise rules alone)",
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


def _extractor(
    choice: str | None,
    samples: Path | None,
    claude_client: ClaudeClient = anthropic.Anthropic,
    meter: Meter = NOT_METERED,
    tracer: tracing.Tracer = tracing.NO_TRACER,
) -> Extractor:
    if choice is None:
        choice = "claude" if samples is None or os.environ.get("ANTHROPIC_API_KEY") else "prepared"
    if choice == "prepared" and samples is not None:
        return load_extractor(samples)
    model = os.environ.get("INVOICE_COLLECTOR_EXTRACTION_MODEL", DEFAULT_MODEL)
    return FallbackExtractor(
        ClaudeExtractor(claude_client(), model, meter, tracer), RuleExtractor(KNOWN_VENDORS)
    )


STRONGER_MODEL = "claude-sonnet-5-5"


def _claude_is_usable(environ: Mapping[str, str], claude_client: ClaudeClient | None) -> bool:
    """Claude can be used with a key in the environment, or with a client that was handed in."""
    return bool(environ.get("ANTHROPIC_API_KEY")) or claude_client is not None


def stronger_extractor_for(
    choice: str | None,
    environ: Mapping[str, str],
    claude_client: ClaudeClient | None = None,
    meter: Meter = NOT_METERED,
    tracer: tracing.Tracer = tracing.NO_TRACER,
) -> Extractor | None:
    """Reads a document again when the first reading is doubted. See ADR 0008."""
    if choice == "prepared" or not _claude_is_usable(environ, claude_client):
        return None
    model = environ.get("INVOICE_COLLECTOR_STRONGER_MODEL", STRONGER_MODEL)
    return ClaudeExtractor((claude_client or anthropic.Anthropic)(), model, meter, tracer)


def classifier_for(
    choice: str | None,
    environ: Mapping[str, str],
    claude_client: ClaudeClient | None = None,
    meter: Meter = NOT_METERED,
    tracer: tracing.Tracer = tracing.NO_TRACER,
) -> FallbackClassifier:
    """The classifier, with those it falls back to when it cannot answer.

    Jev is the default when its key is present: see ADR 0009. Whatever is chosen, the
    classifiers after it in the order Jev, Claude, rules stand behind it.
    """
    available = ["rules"]
    if _claude_is_usable(environ, claude_client):
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
            chain.append(JevClassifier(environ["JEV_API_KEY"], meter=meter, tracer=tracer))
        elif name == "claude":
            client = (claude_client or anthropic.Anthropic)()
            chain.append(ClaudeClassifier(client, model, meter, tracer))
        else:
            chain.append(RuleClassifier())
    return FallbackClassifier(*chain)


def vendor_matcher_for(
    choice: str | None,
    environ: Mapping[str, str],
    claude_client: ClaudeClient | None = None,
    meter: Meter = NOT_METERED,
    tracer: tracing.Tracer = tracing.NO_TRACER,
) -> RulesFirstVendorMatcher:
    """Rules, with the models asked in turn for what the rules cannot decide.

    Jev is asked first when its key is present: see ADR 0009. Whatever is chosen, the
    models after it in the order Jev, Claude stand behind it. Rules alone need no key.
    """
    available = ["rules"]
    if _claude_is_usable(environ, claude_client):
        available.insert(0, "claude")
    if environ.get("JEV_API_KEY"):
        available.insert(0, "jev")
    if choice is None:
        choice = available[0]
    if choice not in available:
        raise SystemExit(f"the {choice} vendor matcher needs its API key in the environment")

    model = environ.get("INVOICE_COLLECTOR_MATCHING_MODEL", CLAUDE_MATCHING_MODEL)
    models: list[VendorMatcher] = []
    for name in available[available.index(choice) : -1]:
        if name == "jev":
            models.append(JevVendorMatcher(environ["JEV_API_KEY"], meter=meter, tracer=tracer))
        else:
            client = (claude_client or anthropic.Anthropic)()
            models.append(ClaudeVendorMatcher(client, model, meter, tracer))
    return RulesFirstVendorMatcher(*models)


def _gmail_source(account: str, token_dir: Path) -> MailSource:
    """A source account read through Gmail with its stored read-only sign-in."""
    return GmailMailSource.signed_in(account, token_dir=token_dir)


def _refusal(args: argparse.Namespace) -> str | None:
    """Why the way of reading mail that was asked for cannot be used, if it cannot."""
    if (args.samples is None) == (not args.account and not args.connected_accounts):
        return (
            "Give either --samples or --account (one or more times) or --connected-accounts, "
            "not both and not neither."
        )
    if (args.account or args.connected_accounts) and args.extractor == "prepared":
        return (
            "--extractor prepared cannot be used with --account: prepared answers exist only "
            "for sample emails. Leave --extractor out to read billing documents with Claude."
        )
    for mapping in args.map:
        sample, _, real = mapping.partition("=")
        if not sample or not real:
            return f"--map {mapping}: give it as SAMPLE=REAL"
    return None


def _addresses(maps: Sequence[str]) -> dict[str, str]:
    """Each sample source account with the real address that receives its emails."""
    addresses: dict[str, str] = {}
    for mapping in maps:
        sample, _, real = mapping.partition("=")
        addresses[sample] = real
    return addresses


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


@contextmanager
def open_pipeline(
    args: argparse.Namespace,
    archive: Archive,
    ledger: Ledger,
    browser: BrowserFactory = HeadlessBrowser,
    *,
    claude_client: ClaudeClient | None = None,
    tracer: tracing.Tracer = tracing.NO_TRACER,
) -> Generator[Pipeline]:
    """The pipeline the parsed options of the collect command describe, open for a run.

    Every model it calls reports to one meter, so the run records what they cost, and
    traces its calls with the tracer.
    """
    meter = RunMeter()
    policy = (
        DestinationPolicy.for_local_pages() if args.allow_local_portals else DestinationPolicy()
    )
    with browser(policy) as opened:
        yield Pipeline(
            classifier=classifier_for(args.classifier, os.environ, claude_client, meter, tracer),
            extractor=_extractor(
                args.extractor, args.samples, claude_client or anthropic.Anthropic, meter, tracer
            ),
            renderer=opened,
            portal_fetcher=opened,
            exchange_rates=(
                NoExchangeRates() if args.no_exchange_rates else FrankfurterExchangeRates()
            ),
            archive=archive,
            ledger=ledger,
            stronger_extractor=stronger_extractor_for(
                args.extractor, os.environ, claude_client, meter, tracer
            ),
            vendor_matcher=vendor_matcher_for(
                args.vendor_matcher, os.environ, claude_client, meter, tracer
            ),
            meter=meter,
        )


def sources(
    args: argparse.Namespace, mail_source_for: MailSourceFor = _gmail_source
) -> list[MailSource]:
    """The source accounts the parsed options of the collect command name.

    Reading through Gmail never opens a browser to sign in: an account without a usable
    sign-in is recorded as not read, and the others are still read.
    """
    if args.samples is not None:
        return list(load_sources(args.samples))
    return [mail_source_for(a, args.token_dir) for a in dict.fromkeys(args.account)]


def main(
    argv: Sequence[str] | None = None,
    google_services: GoogleServices = google_auth.drive_and_sheets_services,
    *,
    digest_sender_for: Callable[[str], DigestSender] = SlackWebhook,
    mail_source_for: MailSourceFor = _gmail_source,
    claude_client: ClaudeClient | None = None,
) -> int:
    if not os.environ.get("INVOICE_COLLECTOR_SKIP_DOTENV"):
        load_dotenv(find_dotenv(usecwd=True))
    args = _parser().parse_args(argv)
    if args.command == "cost":
        return measure_cost(args.month, args.out, args.run)
    return run_collection(
        args.month,
        args,
        google_services,
        digest_sender_for=digest_sender_for,
        mail_source_for=mail_source_for,
        claude_client=claude_client,
    )


def measure_cost(
    month: CollectionMonth, out: Path, run_id: int | None, tracer: tracing.Tracer | None = None
) -> int:
    """Prints what a run's model calls cost, from their traces, beside what the run
    recorded in the ledger. See run_cost.py."""
    ledger = Ledger(out / "ledger.sqlite")
    try:
        runs = [r for r in ledger.runs() if r.collection_month == month]
        chosen = [r for r in runs if run_id is None or r.id == run_id]
        if not chosen:
            print(f"No run of {month} is recorded in {out / 'ledger.sqlite'}.", file=sys.stderr)
            return 2
        run = max(chosen, key=lambda r: r.id)
        measured = (tracer or tracing.tracer_from_environment()).measured(run.id)
        if measured is None:
            print(
                "The cost is read from the run's traces in Langfuse. Set LANGFUSE_PUBLIC_KEY, "
                "LANGFUSE_SECRET_KEY and LANGFUSE_HOST as they were for the run.",
                file=sys.stderr,
            )
            return 1
        # What the run's meter recorded, unless the run was made before runs were metered.
        recorded = run.models if run.models or run.model_cost_usd is not None else None
        cost = run_cost.run_cost(ledger, run.id, month, measured, recorded)
        for line in run_cost.lines(cost):
            print(line)
        return 0
    finally:
        ledger.close()


def run_collection(
    month: CollectionMonth,
    args: argparse.Namespace,
    google_services: GoogleServices = google_auth.drive_and_sheets_services,
    *,
    digest_sender_for: Callable[[str], DigestSender] = SlackWebhook,
    mail_source_for: MailSourceFor = _gmail_source,
    claude_client: ClaudeClient | None = None,
    collector: Collector = collect,
    browser: BrowserFactory = HeadlessBrowser,
    started_by: StartedBy = "command_line",
    tracer: tracing.Tracer | None = None,
) -> int:
    """Everything the collect command does for the month, given its parsed options.

    The collector performs the run itself, and the browser factory opens the browser. The
    tracer traces each model call; by default it is chosen from the environment, and
    traces nothing without Langfuse keys. What it holds is sent before this returns,
    waiting a few seconds at most: a failure to send is a warning, never a failed run.
    """
    refusal = _refusal(args)
    if refusal is not None:
        print(refusal, file=sys.stderr)
        return 2
    if args.connected_accounts:
        args.account += connected_source_accounts(args.out / "ledger.sqlite")
        if not args.account:
            print(
                "No source account is connected. Connect one on the dashboard's Source "
                "accounts screen, or name one with --account.",
                file=sys.stderr,
            )
            return 2
    samples: Path | None = args.samples
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

    expected_vendors: Path | None = args.expected_vendors or (
        samples / "expected_vendors.json" if samples is not None else None
    )
    ledger = Ledger(out / "ledger.sqlite")
    tracer = tracer or tracing.tracer_from_environment()
    try:
        from_file = (
            read_expected_vendors(expected_vendors)
            if expected_vendors is not None and expected_vendors.exists()
            else []
        )
        seed_expected_vendors(ledger, from_file, _addresses(args.map))
        with open_pipeline(
            args, archive, ledger, browser, claude_client=claude_client, tracer=tracer
        ) as pipeline:
            result = collector(
                month,
                sources=sources(args, mail_source_for),
                pipeline=pipeline,
                summary_writers=[CsvSummary(summary_path)],
                settings=Settings(search_window_days=args.search_window_days),
                started_by=started_by,
            )
        report = month_report(ledger, month)
        if sheets is not None:
            # Written after the run, as its other tabs show what the run recorded.
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
        unsent = tracing.flush(tracer)
        if unsent is not None:
            print(f"Warning: {unsent}", file=sys.stderr)

    print(f"Collection month {month}: {len(result.summary)} billing documents collected")
    for state, count in sorted(states.items()):
        print(f"  {state}: {count}")
    print(f"Model cost: {describe_cost(result.model_usage)}")
    print(f"Summary: {summary_path}")
    if sheets is not None:
        print(f"Sheet: {spreadsheet_name(month)}, in Google Drive")
    listed = out / f"{month}_skipped_and_failed.csv"
    with listed.open("w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(skipped_and_failed(report))
    print(f"Skipped and failed: {listed}")
    for email in report.examined_emails:
        if email.state is EmailState.FAILED:
            print(f"Failed: {email.source_account}: {email.subject}: {email.reason}")
    write_gaps(out / f"{month}_gaps.csv", result.gaps)
    for line in gap_lines(result):
        print(line)
    for warning in result.warnings:
        print(f"Warning: {warning}")
    _send_digest(digest_sender, render(digest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
