"""The command that serves the dashboard's API."""

import argparse
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import anthropic
import uvicorn
from dotenv import find_dotenv, load_dotenv
from fastapi import FastAPI
from google.oauth2.credentials import Credentials

from invoice_collector import drive_archive, google_auth
from invoice_collector.api.app import create_app
from invoice_collector.api.collection_runner import (
    LEDGER_FILE,
    RunOptionsRefused,
    dashboard_runner,
)
from invoice_collector.api.identity import GoogleIdentityVerifier, WebClient
from invoice_collector.api.runner_client import RunnerClient
from invoice_collector.api.settings import RUNNER_URL_VARIABLE, Settings, SettingsError
from invoice_collector.api.source_account_connector import GoogleSourceAccountConnector
from invoice_collector.archive import Archive
from invoice_collector.claude_extractor import DEFAULT_MODEL, ClaudeExtractor
from invoice_collector.cli import STRONGER_MODEL, vendor_matcher_for
from invoice_collector.collection_settings import SettingsStore
from invoice_collector.drive_archive import DriveArchiveInChosenFolder
from invoice_collector.exchange_rates import FrankfurterExchangeRates
from invoice_collector.extractor import Extractor, FallbackExtractor
from invoice_collector.ledger import Ledger
from invoice_collector.rule_extractor import RuleExtractor
from invoice_collector.vendor_matcher import VendorMatcher

PORT = 8000

GoogleServices = Callable[[Credentials], tuple[Any, Any]]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="invoice-collector-dashboard", description="Serve the dashboard's API on port 8000"
    )
    parser.add_argument(
        "--ledger", type=Path, required=True, help="the ledger a collection wrote, a SQLite file"
    )
    parser.add_argument(
        "--google-owner",
        metavar="ADDRESS",
        help="the owner account the collection archives to Drive with: a billing document "
        "approved on the Review screen is filed to its Drive too. It must be signed in with "
        "invoice-collector-setup --owner",
    )
    parser.add_argument(
        "--run-options",
        default="",
        metavar="OPTIONS",
        help="options of the collect command for runs started on the Runs screen, in one "
        'quoted text, as --run-options="--no-exchange-rates". The dashboard sets the '
        "source accounts, the output folder and the owner account itself",
    )
    return parser


def _serve(app: FastAPI) -> None:
    uvicorn.run(app, host="127.0.0.1", port=PORT)


def _owner_drive(
    owner: str, token_dir: Path, google_services: GoogleServices, ledger_path: Path
) -> Archive | None:
    """The owner account's Drive archive, or None after saying how to sign it in.

    It files under the Drive folder the settings name when each document is filed."""
    try:
        credentials = google_auth.sign_in(
            owner, drive_archive.SCOPES, token_dir, allow_browser=False
        )
    except google_auth.SignInExpired:
        print(
            f"The dashboard cannot start. The owner account {owner} is not signed in to "
            f"Google Drive, or its sign-in no longer works.\n"
            f"Sign it in with: invoice-collector-setup --owner {owner}",
            file=sys.stderr,
        )
        return None
    drive, _ = google_services(credentials)
    settings = SettingsStore(ledger_path)
    return DriveArchiveInChosenFolder(drive, lambda: settings.read().drive_folder)


def upload_extractors(
    claude: anthropic.Anthropic | None, environment: Mapping[str, str]
) -> tuple[Extractor, Extractor | None]:
    """What reads an uploaded PDF, and what reads it again when that reading is doubted.

    The same models a collection uses. Without a Claude client, rules read it, and what
    rules read is always held for a person to confirm. See ADR 0008.
    """
    rules = RuleExtractor()
    if claude is None:
        return rules, None
    model = environment.get("INVOICE_COLLECTOR_EXTRACTION_MODEL", DEFAULT_MODEL)
    stronger = environment.get("INVOICE_COLLECTOR_STRONGER_MODEL", STRONGER_MODEL)
    return FallbackExtractor(ClaudeExtractor(claude, model), rules), ClaudeExtractor(
        claude, stronger
    )


def upload_vendor_matcher(
    claude: anthropic.Anthropic | None, environment: Mapping[str, str]
) -> VendorMatcher:
    """What matches an uploaded document's vendor to the expected vendor list: the matcher a
    collection builds, rules first, then Jev when its key is set, then Claude. See ADR 0016.
    """
    return vendor_matcher_for(None, environment, (lambda: claude) if claude else None)


def main(
    argv: Sequence[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    google_services: GoogleServices = google_auth.drive_and_sheets_services,
    serve: Callable[[FastAPI], None] = _serve,
) -> int:
    args = _parser().parse_args(argv)
    ledger_path: Path = args.ledger
    if environment is None:
        load_dotenv(find_dotenv(usecwd=True))
        environment = os.environ

    try:
        settings = Settings.from_environment(environment, ledger_path=ledger_path)
        web_client = WebClient.read(settings.web_client_file)
        verifier = GoogleIdentityVerifier(web_client, settings.redirect_uri)
        connector = GoogleSourceAccountConnector(web_client, settings.accounts_redirect_uri)
        api_key = environment.get("ANTHROPIC_API_KEY", "").strip()
        claude = anthropic.Anthropic(api_key=api_key) if api_key else None
        extractor, stronger_extractor = upload_extractors(claude, environment)
        vendor_matcher = upload_vendor_matcher(claude, environment)
        owner_drive: Archive | None = None
        if args.google_owner:
            owner_drive = _owner_drive(
                args.google_owner, settings.token_dir, google_services, ledger_path
            )
            if owner_drive is None:
                return 1
        runs: RunnerClient | None = None
        runner = None
        if settings.runner_url is not None:
            if args.run_options:
                raise RunOptionsRefused(
                    "--run-options are given to the runner service, which performs the runs, "
                    f"when {RUNNER_URL_VARIABLE} is set"
                )
            runs = RunnerClient(settings.runner_url, settings.runner_secret)
        else:
            runner = dashboard_runner(
                ledger_path, settings.token_dir, args.google_owner, args.run_options
            )
        app = create_app(
            settings,
            lambda: Ledger(ledger_path),
            verifier,
            claude=claude,
            source_account_connector=connector,
            exchange_rates=FrankfurterExchangeRates(),
            drive_archive=owner_drive,
            extractor=extractor,
            stronger_extractor=stronger_extractor,
            vendor_matcher=vendor_matcher,
            runner=runner,
            runs=runs,
            schedule_keeper=runs,
        )
    except (SettingsError, RunOptionsRefused) as problem:
        print(f"The dashboard cannot start. {problem}", file=sys.stderr)
        return 2

    if runs is not None:
        print(f"Runs are asked of the runner service at {settings.runner_url}.", file=sys.stderr)
    elif runner is None:
        print(
            f"Warning: the ledger is not named {LEDGER_FILE}, so runs cannot be started "
            "on the Runs screen.",
            file=sys.stderr,
        )
    if claude is None:
        print(
            "Warning: ANTHROPIC_API_KEY is not set, so Ask your invoices is unavailable, "
            "and an uploaded PDF is read by rules and held for review.",
            file=sys.stderr,
        )
    serve(app)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
