"""Command line entry point."""

import argparse
import csv
import os
import sys
from collections import Counter
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from typing import Any, Protocol

import anthropic
from dotenv import find_dotenv, load_dotenv
from google.oauth2.credentials import Credentials

from invoice_collector import drive_archive, google_auth
from invoice_collector.api.settings import PUBLIC_URL_VARIABLE, SettingsError, parse_public_url
from invoice_collector.archive import Archive, BothArchives, LocalArchive
from invoice_collector.browser import (
    BrowserFactory,
    HeadlessBrowser,
    ThreadConfinedBrowser,
)
from invoice_collector.classifier import Classifier, FallbackClassifier, RuleClassifier
from invoice_collector.claude_classifier import ClaudeClassifier
from invoice_collector.claude_extractor import DEFAULT_MODEL, ClaudeExtractor
from invoice_collector.claude_vendor_matcher import DEFAULT_MODEL as CLAUDE_MATCHING_MODEL
from invoice_collector.claude_vendor_matcher import ClaudeVendorMatcher
from invoice_collector.collection_settings import SettingsStore
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
from invoice_collector.drive_archive import DEFAULT_ROOT_FOLDER, DriveArchive
from invoice_collector.exchange_rates import FrankfurterExchangeRates, NoExchangeRates
from invoice_collector.extractor import Extractor, FallbackExtractor
from invoice_collector.gap_report import lines as gap_lines
from invoice_collector.gap_report import read_expected_vendors, write_gaps
from invoice_collector.gmail_source import GmailMailSource
from invoice_collector.jev_classifier import JevClassifier
from invoice_collector.ledger import Ledger
from invoice_collector.mail_source import MailSource
from invoice_collector.metering import NOT_METERED, Meter, RunMeter, describe_cost
from invoice_collector.rule_extractor import RuleExtractor
from invoice_collector.run import Pipeline, RunResult, Settings, collect, seed_expected_vendors
from invoice_collector.samples import load_sources
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

# The ledger a run writes, in the folder given with --out.
LEDGER_FILE = "ledger.sqlite"

# Drive and Sheets clients acting as the owner account.
GoogleServices = Callable[[Credentials], tuple[Any, Any]]
SLACK_WEBHOOK_VARIABLE = "INVOICE_COLLECTOR_SLACK_WEBHOOK"
# The dashboard reads the same variable, so both look in one folder.
TOKEN_DIR_VARIABLE = "INVOICE_COLLECTOR_TOKEN_DIR"
# The mail source of one source account, given the folder of stored sign-ins.
MailSourceFor = Callable[[str, Path], MailSource]
ClaudeClient = Callable[[], anthropic.Anthropic]


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
        default=Path(os.environ.get(TOKEN_DIR_VARIABLE) or google_auth.DEFAULT_TOKEN_DIR),
        help=f"where stored Google sign-ins are kept (default: {TOKEN_DIR_VARIABLE} when it "
        "is set, as for the dashboard)",
    )
    collect_cmd.add_argument(
        "--no-digest",
        action="store_true",
        help=f"do not send the digest to Slack, even when {SLACK_WEBHOOK_VARIABLE} is set",
    )
    collect_cmd.add_argument(
        "--max-concurrent",
        type=_at_least_one,
        default=Settings().at_once,
        metavar="N",
        help="how many emails to fetch and read at once, on threads of their own; this is "
        "also the ceiling on model calls in flight. Each is still weighed against the "
        "ledger, filed and recorded one at a time (default: 1, one after another)",
    )


def _at_least_one(text: str) -> int:
    try:
        number = int(text)
    except ValueError:
        number = 0
    if number < 1:
        raise argparse.ArgumentTypeError(f"give a whole number of 1 or more, not {text}")
    return number


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


STRONGER_MODEL = "claude-sonnet-5-5"


def _claude_is_usable(environ: Mapping[str, str], claude_client: ClaudeClient | None) -> bool:
    """Claude can be used with a key in the environment, or with a client that was handed in."""
    return bool(environ.get("ANTHROPIC_API_KEY")) or claude_client is not None


def extractor_for(
    environ: Mapping[str, str],
    claude_client: ClaudeClient | None = None,
    meter: Meter = NOT_METERED,
) -> Extractor:
    """Claude, with rules behind it; rules alone when Claude cannot be used.

    What rules read is always held for a person to confirm. See ADR 0008.
    """
    rules = RuleExtractor()
    if not _claude_is_usable(environ, claude_client):
        return rules
    model = environ.get("INVOICE_COLLECTOR_EXTRACTION_MODEL", DEFAULT_MODEL)
    claude = (claude_client or anthropic.Anthropic)()
    return FallbackExtractor(ClaudeExtractor(claude, model, meter), rules)


def stronger_extractor_for(
    environ: Mapping[str, str],
    claude_client: ClaudeClient | None = None,
    meter: Meter = NOT_METERED,
) -> Extractor | None:
    """Reads a document again when the first reading is doubted. See ADR 0008."""
    if not _claude_is_usable(environ, claude_client):
        return None
    model = environ.get("INVOICE_COLLECTOR_STRONGER_MODEL", STRONGER_MODEL)
    return ClaudeExtractor((claude_client or anthropic.Anthropic)(), model, meter)


def classifier_for(
    choice: str | None,
    environ: Mapping[str, str],
    claude_client: ClaudeClient | None = None,
    meter: Meter = NOT_METERED,
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
            chain.append(JevClassifier(environ["JEV_API_KEY"], meter=meter))
        elif name == "claude":
            chain.append(ClaudeClassifier((claude_client or anthropic.Anthropic)(), model, meter))
        else:
            chain.append(RuleClassifier())
    return FallbackClassifier(*chain)


def vendor_matcher_for(
    choice: str | None,
    environ: Mapping[str, str],
    claude_client: ClaudeClient | None = None,
    meter: Meter = NOT_METERED,
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
            models.append(JevVendorMatcher(environ["JEV_API_KEY"], meter=meter))
        else:
            claude = (claude_client or anthropic.Anthropic)()
            models.append(ClaudeVendorMatcher(claude, model, meter))
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
    extractor: Extractor | None = None,
) -> Generator[Pipeline]:
    """The pipeline the parsed options of the collect command describe, open for a run.

    Every model it calls reports to one meter, so the run records what they cost. An
    extractor handed in, as tests hand in the answers prepared with the sample mail, reads
    every document in place of the models and rules, with nothing to read again behind it.
    """
    meter = RunMeter()
    policy = (
        DestinationPolicy.for_local_pages() if args.allow_local_portals else DestinationPolicy()
    )
    with browser(policy) as opened:
        yield Pipeline(
            classifier=classifier_for(args.classifier, os.environ, claude_client, meter),
            extractor=extractor or extractor_for(os.environ, claude_client, meter),
            renderer=opened,
            portal_fetcher=opened,
            exchange_rates=(
                NoExchangeRates() if args.no_exchange_rates else FrankfurterExchangeRates()
            ),
            archive=archive,
            ledger=ledger,
            stronger_extractor=(
                None
                if extractor is not None
                else stronger_extractor_for(os.environ, claude_client, meter)
            ),
            vendor_matcher=vendor_matcher_for(
                args.vendor_matcher, os.environ, claude_client, meter
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
    extractor: Extractor | None = None,
) -> int:
    if not os.environ.get("INVOICE_COLLECTOR_SKIP_DOTENV"):
        load_dotenv(find_dotenv(usecwd=True))
    args = _parser().parse_args(argv)
    return run_collection(
        args.month,
        args,
        google_services,
        digest_sender_for=digest_sender_for,
        mail_source_for=mail_source_for,
        claude_client=claude_client,
        extractor=extractor,
    )


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
    extractor: Extractor | None = None,
) -> int:
    """Everything the collect command does for the month, given its parsed options.

    The collector performs the run itself, and the browser factory opens the browser. An
    extractor, when given, reads every document in place of the models and rules.
    """
    refusal = _refusal(args)
    if refusal is not None:
        print(refusal, file=sys.stderr)
        return 2
    try:
        # The digest and the sheet link to the dashboard where people open it.
        dashboard_url = parse_public_url(os.environ.get(PUBLIC_URL_VARIABLE, ""))
    except SettingsError as problem:
        print(problem, file=sys.stderr)
        return 2
    if args.connected_accounts:
        args.account += connected_source_accounts(args.out / LEDGER_FILE)
        if not args.account:
            print(
                "No source account is connected. Connect one on the dashboard's Source "
                "accounts screen, or name one with --account.",
                file=sys.stderr,
            )
            return 2
    samples: Path | None = args.samples
    out: Path = args.out
    at_once: int = args.max_concurrent
    if at_once > 1:
        # Emails fetched on several threads share one browser, which Playwright allows
        # only from the thread that started it.
        browser = partial(ThreadConfinedBrowser, browser=browser)
    summary_path = out / f"{month}_summary.csv"
    digest_sender = _digest_sender(args.no_digest, digest_sender_for)

    archive: Archive = LocalArchive(out / "archive")
    drive: Any = None
    sheets: Any = None
    drive_folder = DEFAULT_ROOT_FOLDER
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
        # Drive comes first, so the summary links to each PDF in Drive. The folder is the
        # one chosen on the Settings screen when the run starts.
        drive_folder = SettingsStore(out / LEDGER_FILE).read().drive_folder
        archive = BothArchives(DriveArchive(drive, drive_folder), archive)

    expected_vendors: Path | None = args.expected_vendors or (
        samples / "expected_vendors.json" if samples is not None else None
    )
    ledger = Ledger(out / LEDGER_FILE)
    try:
        from_file = (
            read_expected_vendors(expected_vendors)
            if expected_vendors is not None and expected_vendors.exists()
            else []
        )
        seed_expected_vendors(ledger, from_file, _addresses(args.map))
        with open_pipeline(
            args, archive, ledger, browser, claude_client=claude_client, extractor=extractor
        ) as pipeline:
            result = collector(
                month,
                sources=sources(args, mail_source_for),
                pipeline=pipeline,
                summary_writers=[CsvSummary(summary_path)],
                settings=Settings(search_window_days=args.search_window_days, at_once=at_once),
                started_by=started_by,
            )
        report = month_report(ledger, month)
        if sheets is not None:
            # Written after the run, as its other tabs show what the run recorded.
            SheetSummary(
                sheets,
                drive,
                month,
                report,
                dashboard_url=dashboard_url,
                root_folder=drive_folder,
            ).write(result.summary)
        states = Counter(e.state.value for e in ledger.examined_emails(month))
        digest = build_digest(
            month,
            result,
            ledger,
            gaps=[(g.vendor, g.kind, g.explanation) for g in result.gaps],
            failed_source_accounts=sorted(result.failed_source_accounts),
            summary_link=str(summary_path),
            dashboard_url=dashboard_url,
        )
    except Exception as error:
        _send_digest(digest_sender, render_failure(month, str(error) or type(error).__name__))
        raise
    finally:
        ledger.close()

    print(f"Collection month {month}: {len(result.summary)} billing documents collected")
    for state, count in sorted(states.items()):
        print(f"  {state}: {count}")
    print(f"Model cost: {describe_cost(result.model_usage)}")
    print(f"Summary: {summary_path}")
    if sheets is not None:
        print(f"Sheet: {spreadsheet_name(month)}, in {drive_folder} in Google Drive")
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
