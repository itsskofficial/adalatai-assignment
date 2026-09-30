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
    LEDGER_FILE,
    CollectionRunner,
    RunCollection,
    RunOptionsRefused,
    parse_run_options,
)
from invoice_collector.api.settings import (
    DEFAULT_TOKEN_DIR,
    PUBLIC_URL_VARIABLE,
    TOKEN_DIR_VARIABLE,
    SettingsError,
    parse_public_url,
)
from invoice_collector.cli import run_collection
from invoice_collector.collection_settings import SettingsStore
from invoice_collector.destinations import sample_portal_problems
from invoice_collector.ledger import Ledger
from invoice_collector.owner_account import GOOGLE_OWNER_VARIABLE, owner_state, said_at_start
from invoice_collector.run_requests import RunRequests
from invoice_collector.run_starter import RunStarter
from invoice_collector.runner.http_api import runner_app
from invoice_collector.runner.service import RunnerService, TimerFactory, threading_timer
from invoice_collector.startup import ledger_problem, refuse

SECRET_VARIABLE = "INVOICE_COLLECTOR_RUNNER_SECRET"
HOST_VARIABLE = "INVOICE_COLLECTOR_RUNNER_HOST"
PORT_VARIABLE = "INVOICE_COLLECTOR_RUNNER_PORT"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8001

Serve = Callable[[FastAPI, str, int], None]


def _parser(environment: Mapping[str, str]) -> argparse.ArgumentParser:
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
        default=environment.get(GOOGLE_OWNER_VARIABLE, "").strip() or None,
        help="the owner account runs archive to Drive with while none is chosen on the "
        "dashboard's Source accounts screen; the one chosen there wins. It is signed in with "
        f"access to the Drive files the tool creates (default: {GOOGLE_OWNER_VARIABLE})",
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
    if environment is None:
        if not os.environ.get("INVOICE_COLLECTOR_SKIP_DOTENV"):
            load_dotenv(find_dotenv(usecwd=True))
        environment = os.environ
    args = _parser(environment).parse_args(argv)
    ledger_path: Path = args.ledger
    problems: list[str] = []
    secret = environment.get(SECRET_VARIABLE, "")
    if not secret.strip():
        problems.append(
            f"{SECRET_VARIABLE} is not set. The dashboard sends it with every request, "
            "so reaching the runner's port is not enough to start a run. Set it to a "
            "long random value, the same for the runner and the dashboard."
        )
    host = environment.get(HOST_VARIABLE, "").strip() or DEFAULT_HOST
    port = DEFAULT_PORT
    try:
        port = _port(environment.get(PORT_VARIABLE, "").strip() or str(DEFAULT_PORT))
    except RunnerSettingsError as problem:
        problems.append(str(problem))
    try:
        # A run links its digest and sheet to the dashboard at this address.
        parse_public_url(environment.get(PUBLIC_URL_VARIABLE, ""))
    except SettingsError as problem:
        problems += problem.problems
    # A run reads them to know which local address it may open for sample mail.
    problems += sample_portal_problems(environment)
    unwritable = ledger_problem(ledger_path)
    if unwritable is not None:
        problems.append(unwritable)
    options: list[str] = []
    try:
        options = parse_run_options(args.run_options)
    except RunOptionsRefused as refused:
        problems.append(f"The run options are refused: {refused}")
    if ledger_path.name != LEDGER_FILE:
        problems.append(
            f"The ledger must be named {LEDGER_FILE}, in the folder runs write to: a run "
            f"writes its ledger as {LEDGER_FILE} there."
        )
    if problems:
        refuse("runner", problems)
        return 2
    token_dir = Path(environment.get(TOKEN_DIR_VARIABLE) or DEFAULT_TOKEN_DIR)
    runner = CollectionRunner(
        ledger_path, token_dir, google_owner=args.google_owner, options=options, run=run
    )

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
    # Said, not refused: the owner account is connected on the dashboard, which waits for
    # the runner. Each run looks the owner account up again when it starts.
    for warning in said_at_start(
        owner_state(ledger_path, args.google_owner, token_dir),
        "The runner starts, and each run stops before collecting, saying so in its digest "
        "and on the Runs screen, until it is signed in.",
    ):
        print(warning, file=sys.stderr)
    service.start()
    try:
        serve(app, host, port)
    finally:
        service.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
