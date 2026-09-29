"""The run the dashboard starts: the collect command's, given the options it would be given.

ADR 0010: the schedule, the dashboard and the command line start one function and it
behaves the same whichever started it. So a run from the dashboard is run_collection,
with the collect command's options, reading the source accounts connected in the ledger
as `--connected-accounts` does, or the one failed source account being run again. It
writes the archive, the summary, the Google Sheet and the Slack digest as any run does.
"""

import argparse
import shlex
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from invoice_collector.cli import add_collection_options, run_collection
from invoice_collector.domain import CollectionMonth, StartedBy

# Where the collect command keeps its ledger, inside the folder given with --out.
LEDGER_FILE = "ledger.sqlite"

# Options the dashboard decides itself, and so may not be given among the run options.
DECIDED_BY_THE_DASHBOARD = (
    "--samples",
    "--account",
    "--connected-accounts",
    "--out",
    "--token-dir",
    "--google-owner",
)


class RunCollection(Protocol):
    """Everything the collect command does for a month, as cli.run_collection does it."""

    def __call__(
        self, month: CollectionMonth, args: argparse.Namespace, *, started_by: StartedBy
    ) -> int: ...


class RunOptionsRefused(Exception):
    """The run options cannot be used. The message says why."""


class RunNotCompleted(Exception):
    """The run stopped before it completed. The message says why, as far as is known."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="collect", add_help=False, exit_on_error=False)
    parser.add_argument("month", type=CollectionMonth.parse)
    add_collection_options(parser)
    return parser


def parse_run_options(text: str) -> list[str]:
    """The collect command's options given to the dashboard, checked before it starts."""
    try:
        options = shlex.split(text)
    except ValueError as problem:
        raise RunOptionsRefused(f"the run options cannot be read: {problem}") from None
    for option in options:
        name = option.split("=", 1)[0]
        if name in DECIDED_BY_THE_DASHBOARD:
            raise RunOptionsRefused(
                f"{name} cannot be among the run options: the dashboard sets it itself"
            )
    try:
        _parser().parse_args(["2026-01", *options])
    except (argparse.ArgumentError, SystemExit) as problem:
        raise RunOptionsRefused(
            f"the run options are not the collect command's: {problem}"
        ) from None
    return options


def dashboard_runner(
    ledger_path: Path, token_dir: Path, google_owner: str | None, run_options: str
) -> "CollectionRunner | None":
    """The runner for the dashboard's ledger, or None when a run cannot write to it.

    Raises RunOptionsRefused when the run options cannot be used.
    """
    options = parse_run_options(run_options)
    if ledger_path.name != LEDGER_FILE:
        return None
    return CollectionRunner(ledger_path, token_dir, google_owner=google_owner, options=options)


class CollectionRunner:
    """Runs a collection month as the collect command does, started from the dashboard or,
    in the runner service, by the schedule."""

    def __init__(
        self,
        ledger_path: Path,
        token_dir: Path,
        *,
        google_owner: str | None = None,
        options: Sequence[str] = (),
        run: RunCollection = run_collection,
    ) -> None:
        if ledger_path.name != LEDGER_FILE:
            raise RunOptionsRefused(
                f"a run writes its ledger as {LEDGER_FILE} in its output folder, so the "
                f"dashboard can start runs only when its ledger is named {LEDGER_FILE}"
            )
        self._out = ledger_path.parent
        self._token_dir = token_dir
        self._google_owner = google_owner
        self._options = list(options)
        self._run = run

    def arguments(self, month: CollectionMonth, source_account: str | None) -> list[str]:
        """The collect command's arguments for the run."""
        arguments = [str(month), "--out", str(self._out), "--token-dir", str(self._token_dir)]
        if self._google_owner:
            arguments += ["--google-owner", self._google_owner]
        if source_account is None:
            arguments.append("--connected-accounts")
        else:
            arguments += ["--account", source_account]
        return arguments + self._options

    def __call__(
        self,
        month: CollectionMonth,
        source_account: str | None,
        *,
        started_by: StartedBy = "dashboard",
    ) -> None:
        args = _parser().parse_args(self.arguments(month, source_account))
        exit_code = self._run(month, args, started_by=started_by)
        if exit_code != 0:
            raise RunNotCompleted(
                f"the run stopped before collecting anything (exit code {exit_code}); the "
                "log of the service that performed it says why"
            )
