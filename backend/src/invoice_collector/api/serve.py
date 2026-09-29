"""The command that serves the dashboard's API."""

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import anthropic
import uvicorn
from dotenv import find_dotenv, load_dotenv

from invoice_collector.api.app import create_app
from invoice_collector.api.identity import GoogleIdentityVerifier, WebClient
from invoice_collector.api.settings import Settings, SettingsError
from invoice_collector.api.source_account_connector import GoogleSourceAccountConnector
from invoice_collector.ledger import Ledger

PORT = 8000


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="invoice-collector-dashboard", description="Serve the dashboard's API on port 8000"
    )
    parser.add_argument(
        "--ledger", type=Path, required=True, help="the ledger a collection wrote, a SQLite file"
    )
    return parser


def main(argv: Sequence[str] | None = None, *, environment: Mapping[str, str] | None = None) -> int:
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
        app = create_app(
            settings,
            lambda: Ledger(ledger_path),
            verifier,
            claude=claude,
            source_account_connector=connector,
        )
    except SettingsError as problem:
        print(f"The dashboard cannot start. {problem}", file=sys.stderr)
        return 2

    if claude is None:
        print(
            "Warning: ANTHROPIC_API_KEY is not set, so Ask your invoices is unavailable.",
            file=sys.stderr,
        )
    uvicorn.run(app, host="127.0.0.1", port=PORT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
