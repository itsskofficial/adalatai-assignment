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
from invoice_collector.api.identity import GoogleIdentityVerifier, WebClient
from invoice_collector.api.settings import Settings, SettingsError
from invoice_collector.api.source_account_connector import GoogleSourceAccountConnector
from invoice_collector.archive import Archive
from invoice_collector.drive_archive import DriveArchive
from invoice_collector.exchange_rates import FrankfurterExchangeRates
from invoice_collector.ledger import Ledger

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
    return parser


def _serve(app: FastAPI) -> None:
    uvicorn.run(app, host="127.0.0.1", port=PORT)


def _owner_drive(owner: str, token_dir: Path, google_services: GoogleServices) -> Archive | None:
    """The owner account's Drive archive, or None after saying how to sign it in."""
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
    return DriveArchive(drive)


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
        owner_drive: Archive | None = None
        if args.google_owner:
            owner_drive = _owner_drive(args.google_owner, settings.token_dir, google_services)
            if owner_drive is None:
                return 1
        app = create_app(
            settings,
            lambda: Ledger(ledger_path),
            verifier,
            claude=claude,
            source_account_connector=connector,
            exchange_rates=FrankfurterExchangeRates(),
            drive_archive=owner_drive,
        )
    except SettingsError as problem:
        print(f"The dashboard cannot start. {problem}", file=sys.stderr)
        return 2

    if claude is None:
        print(
            "Warning: ANTHROPIC_API_KEY is not set, so Ask your invoices is unavailable.",
            file=sys.stderr,
        )
    serve(app)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
