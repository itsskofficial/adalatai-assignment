"""The command that starts the runner service: invoice-collector-runner.

It performs every run the dashboard asks for, and the scheduled runs, by calling what the
collect command calls (ADR 0010). It listens on the private network between it and the app,
on localhost unless told otherwise, and serves no pages.
"""

import argparse
import logging
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import uvicorn
from dotenv import find_dotenv, load_dotenv
from fastapi import FastAPI

from invoice_collector.api.collection_runner import (
    CollectionRunner,
    RunCollection,
    RunOptionsRefused,
    parse_run_options,
)
from invoice_collector.api.settings import DEFAULT_TOKEN_DIR, TOKEN_DIR_VARIABLE
from invoice_collector.cli import run_collection
from invoice_collector.collection_settings import SettingsStore
from invoice_collector.ledger import Ledger
from invoice_collector.run_requests import RunRequests
from invoice_collector.run_starter import RunStarter
from invoice_collector.runner.http_api import runner_app
from invoice_collector.runner.service import RunnerService, TimerFactory, threading_timer

SECRET_VARIABLE = "INVOICE_COLLECTOR_RUNNER_SECRET"
HOST_VARIABLE = "INVOICE_COLLECTOR_RUNNER_HOST"
PORT_VARIABLE = "INVOICE_COLLECTOR_RUNNER_PORT"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8001

Serve = Callable[[FastAPI, str, int], None]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="invoice-collector-runner",
        description="Perform the runs the dashboard asks for, and the scheduled runs",
    )
    parser.add_argument(
        "--ledger",
        type=Path,
        required=True,
        help="the ledger runs write, named ledger.sqlite, in the folder runs write to; the "
        "dashboard's ledger",
    )
    parser.add_argument(
        "--google-owner",
        metavar="ADDRESS",
        help="the owner account runs archive to Drive with. It must be signed in with "
        "invoice-collector-setup --owner",
    )
    parser.add_argument(
        "--run-options",
        default="",
        metavar="OPTIONS",
        help="options of the collect command for every run, in one quoted text, as "
        '--run-options="--max-concurrent 5". The runner sets the source accounts, the '
        "output folder and the owner account itself",
    )
    return parser


def _serve(app: FastAPI, host: str, port: int) -> None:
    uvicorn.run(app, host=host, port=port)


class RunnerSettingsError(Exception):
    """The runner cannot start with the settings it was given."""


def _port(text: str) -> int:
    try:
        port = int(text)
    except ValueError:
        port = 0
    if not 1 <= port <= 65535:
        raise RunnerSettingsError(f"{PORT_VARIABLE} must be a port number, not {text}.")
    return port


def main(
    argv: Sequence[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    serve: Serve = _serve,
    run: RunCollection = run_collection,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    timer: TimerFactory = threading_timer,
) -> int:
    args = _parser().parse_args(argv)
    if environment is None:
        if not os.environ.get("INVOICE_COLLECTOR_SKIP_DOTENV"):
            load_dotenv(find_dotenv(usecwd=True))
        environment = os.environ
    ledger_path: Path = args.ledger
    try:
        secret = environment.get(SECRET_VARIABLE, "")
        if not secret.strip():
            raise RunnerSettingsError(
                f"{SECRET_VARIABLE} is not set. The dashboard sends it with every request, "
                "so reaching the runner's port is not enough to start a run. Set it to a "
                "long random value, the same for the runner and the dashboard."
            )
        host = environment.get(HOST_VARIABLE, "").strip() or DEFAULT_HOST
        port = _port(environment.get(PORT_VARIABLE, "").strip() or str(DEFAULT_PORT))
        token_dir = Path(environment.get(TOKEN_DIR_VARIABLE) or DEFAULT_TOKEN_DIR)
        runner = CollectionRunner(
            ledger_path,
            token_dir,
            google_owner=args.google_owner,
            options=parse_run_options(args.run_options),
            run=run,
        )
    except (RunnerSettingsError, RunOptionsRefused) as problem:
        print(f"The runner cannot start. {problem}", file=sys.stderr)
        return 2

    def ledger() -> Ledger:
        return Ledger(ledger_path)

    starter = RunStarter(
        runner,
        RunRequests(ledger_path),
        ledger,
        clock,
        performs=frozenset({"dashboard", "schedule"}),
    )
    service = RunnerService(starter, SettingsStore(ledger_path), ledger, clock, timer)
    app = runner_app(service, secret)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    service.start()
    try:
        serve(app, host, port)
    finally:
        service.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
