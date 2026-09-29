"""Runs collections as Prefect flows, one task per email, on a monthly schedule.

This is the thin layer over invoice_collector.run that ADR 0003 describes. The run
knows nothing of Prefect: it hands its examinations to the examine_all given here,
which performs each as a Prefect task. `invoice-collector collect` runs the same
collection without Prefect.
"""

import argparse
import os
import sys
import threading
from collections import Counter
from collections.abc import Callable, Generator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from functools import partial
from types import TracebackType
from typing import Any, Self
from zoneinfo import ZoneInfo

from dotenv import find_dotenv, load_dotenv
from prefect import flow, task
from prefect.artifacts import create_markdown_artifact
from prefect.cache_policies import NO_CACHE
from prefect.client.orchestration import get_client
from prefect.client.schemas.objects import State, TaskRun
from prefect.futures import PrefectFuture, wait
from prefect.runtime import flow_run
from prefect.schedules import Cron
from prefect.settings import PREFECT_API_URL, PREFECT_SERVER_EPHEMERAL_ENABLED, temporary_settings
from prefect.task_runners import ThreadPoolTaskRunner
from prefect.tasks import Task, exponential_backoff
from prefect.types.entrypoint import EntrypointType

from invoice_collector.archive import Archive
from invoice_collector.browser import HeadlessBrowser
from invoice_collector.cli import Browser, BrowserFactory, add_collection_options, run_collection
from invoice_collector.destinations import DestinationPolicy
from invoice_collector.domain import CollectionMonth, Email
from invoice_collector.mail_source import MailSource
from invoice_collector.portal import LoginGated
from invoice_collector.run import (
    Examination,
    Pipeline,
    RunResult,
    Settings,
    collect,
    failure_reason,
    worth_retrying,
)
from invoice_collector.summary import SummaryWriter

# How many emails are examined at once. Each examination calls the model once or twice,
# so this is also the ceiling on concurrent model calls (docs/cost-and-latency.md).
DEFAULT_MAX_CONCURRENT = 5
DEFAULT_RETRIES = 2
# 10 seconds, then 20, each varied at random by up to half as much again, so emails
# that failed together do not all come back at the same moment.
_BACKOFF_SECONDS = 10
_JITTER = 0.5

# The 3rd of each month at 06:00 in India: late enough that vendors billing on the 1st
# have sent their invoices, early enough to act on gaps.
MONTHLY = Cron("0 6 3 * *", timezone="Asia/Kolkata")
_INDIA = ZoneInfo("Asia/Kolkata")
_SUBJECT_LENGTH = 40


def month_before(scheduled: datetime) -> CollectionMonth:
    """The collection month a run scheduled for this time collects: the month before."""
    day = scheduled.astimezone(_INDIA).date()
    if day.month == 1:
        return CollectionMonth(day.year - 1, 12)
    return CollectionMonth(day.year, day.month - 1)


def _worth_retrying(task: Task[..., Any], task_run: TaskRun, state: State[Any]) -> bool:
    error = state.result(raise_on_failure=False)
    return not isinstance(error, BaseException) or worth_retrying(error)


# Why a task is retried, and what for.
#
# An examination records every outcome the pipeline anticipates in the ledger, failures
# included: a classifier that is down, a PDF nothing can read, a portal that will not
# open. It then returns normally, the task completes, and nothing is retried. Those are
# final outcomes, and the pipeline has already made its own attempts (fallback
# classifiers and extractors) before recording them.
#
# An examination raises only when something unanticipated goes wrong: a connection
# reset while fetching, a disk that is briefly full, the ledger busy. Then nothing has
# been recorded for the email (the ledger records an outcome in one transaction, as the
# last step), so performing the examination again is safe, and it is what a task-level
# retry is for. Failures of the code itself are not retried, since they would only
# fail again. When the retries are spent, the email is recorded as failed with the
# reason, and the other emails carry on.
@task(cache_policy=NO_CACHE, retry_condition_fn=_worth_retrying)
def _examine(examination: Examination, one_at_a_time: threading.Lock) -> None:
    # Emails that are copies of each other are examined one after the other, so the
    # second finds the document the first collected instead of reading it again.
    with one_at_a_time:
        examination()


def _task_run_name(email: Email) -> str:
    subject = " ".join(email.subject.split())
    if len(subject) > _SUBJECT_LENGTH:
        subject = subject[: _SUBJECT_LENGTH - 1].rstrip() + "…"
    return f"{email.source_account}: {subject}"


def _copies_key(email: Email) -> tuple[str, str]:
    """The same email delivered to several source accounts shares its sender and subject."""
    return (email.sender.casefold(), " ".join(email.subject.split()).casefold())


def _examine_as_tasks(
    examinations: Sequence[Examination],
    *,
    max_concurrent: int,
    retries: int,
    retry_delay_seconds: float | Sequence[float],
) -> None:
    locks: dict[tuple[str, str], threading.Lock] = {}
    for examination in examinations:
        locks.setdefault(_copies_key(examination.email), threading.Lock())

    examine = _examine.with_options(
        retries=retries,
        retry_delay_seconds=(
            retry_delay_seconds
            if isinstance(retry_delay_seconds, float | int)
            else list(retry_delay_seconds)
        ),
        retry_jitter_factor=_JITTER,
    )
    runner: ThreadPoolTaskRunner[None] = ThreadPoolTaskRunner(max_workers=max_concurrent)
    with runner:
        futures: list[PrefectFuture[None]] = [
            runner.submit(
                examine.with_options(task_run_name=_task_run_name(examination.email)),
                parameters={
                    "examination": examination,
                    "one_at_a_time": locks[_copies_key(examination.email)],
                },
            )
            for examination in examinations
        ]
        wait(futures)

    for examination, future in zip(examinations, futures, strict=True):
        if future.state.is_failed():
            error = future.result(raise_on_failure=False)
            examination.fail(failure_reason(error))


class _OneSaveAtATime:
    """An archive saved to from several threads, one save at a time.

    Google's API client may not be used from several threads at once, and a local save
    checks for a file before writing it.
    """

    def __init__(self, archive: Archive) -> None:
        self._archive = archive
        self._lock = threading.Lock()

    def save(self, folder: str, filename: str, pdf: bytes) -> str:
        with self._lock:
            return self._archive.save(folder, filename, pdf)

    def remove(self, folder: str, filename: str, pdf: bytes) -> None:
        with self._lock:
            self._archive.remove(folder, filename, pdf)


def run_summary(month: CollectionMonth, result: RunResult, states: dict[str, int]) -> str:
    """The run in markdown, as shown in the Prefect interface."""
    lines = [
        f"# Collection month {month}",
        "",
        "| | Count |",
        "|---|---|",
        f"| Billing documents collected | {len(result.summary)} |",
        *(f"| {state} | {count} |" for state, count in sorted(states.items())),
        "",
        "## Gaps",
        "",
    ]
    if result.gaps:
        lines += ["| Vendor | Gap | Source account | Explanation |", "|---|---|---|---|"]
        lines += [
            f"| {g.vendor} | {g.kind} | {g.source_account or ''} | {g.explanation or ''} |"
            for g in result.gaps
        ]
    else:
        lines.append("None.")
    lines += ["", "## Source accounts that could not be read", ""]
    if result.failed_source_accounts:
        lines += [
            f"- {account}: {reason}"
            for account, reason in sorted(result.failed_source_accounts.items())
        ]
    else:
        lines.append("None.")
    lines += ["", "## Warnings", ""]
    lines += [f"- {warning}" for warning in result.warnings] or ["None."]
    return "\n".join(lines) + "\n"


class RunInputs:
    """What invoice_collector.run.collect is given besides the month.

    Handed to the flow as one plain object: Prefect describes every flow parameter
    with a schema, and cannot describe the protocols these are typed with.
    """

    def __init__(
        self,
        *,
        sources: Sequence[MailSource],
        pipeline: Pipeline,
        summary_writers: Sequence[SummaryWriter],
        settings: Settings | None = None,
    ) -> None:
        self.sources = sources
        self.pipeline = pipeline
        self.summary_writers = summary_writers
        self.settings = settings

    def __repr__(self) -> str:
        accounts = ", ".join(source.source_account for source in self.sources)
        return f"RunInputs(source accounts: {accounts})"


@flow(
    name="collect-month",
    flow_run_name="collect-month-{month}",
    validate_parameters=False,
)
def collect_month(
    month: CollectionMonth,
    inputs: RunInputs,
    *,
    max_concurrent: int = DEFAULT_MAX_CONCURRENT,
    retries: int = DEFAULT_RETRIES,
    retry_delay_seconds: float | Sequence[float] | None = None,
) -> RunResult:
    """Collects the month as invoice_collector.run.collect does, one task per email."""
    delays = (
        retry_delay_seconds
        if retry_delay_seconds is not None
        else exponential_backoff(backoff_factor=_BACKOFF_SECONDS)(retries)
    )
    pipeline = inputs.pipeline
    result = collect(
        month,
        sources=inputs.sources,
        pipeline=replace(pipeline, archive=_OneSaveAtATime(pipeline.archive)),
        summary_writers=inputs.summary_writers,
        settings=inputs.settings,
        examine_all=partial(
            _examine_as_tasks,
            max_concurrent=max_concurrent,
            retries=retries,
            retry_delay_seconds=delays,
        ),
    )
    states = Counter(e.state.value for e in pipeline.ledger.examined_emails(month))
    create_markdown_artifact(
        run_summary(month, result, dict(states)),
        key=f"collection-{month}",
        description=f"What the run for collection month {month} found",
    )
    return result


class ThreadConfinedBrowser:
    """A browser kept on a thread of its own, used from any thread one call at a time.

    Playwright's sync API may only be used from the thread that started it; a call
    from another thread fails even when calls never overlap. So the browser is started,
    used and stopped on one thread, and every call is handed to that thread.
    """

    def __init__(
        self, policy: DestinationPolicy, browser: BrowserFactory = HeadlessBrowser
    ) -> None:
        self._opening = browser(policy)
        self._thread = ThreadPoolExecutor(max_workers=1, thread_name_prefix="browser")

    def __enter__(self) -> Self:
        self._browser: Browser = self._thread.submit(self._opening.__enter__).result()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            self._thread.submit(self._opening.__exit__, exc_type, exc, traceback).result()
        finally:
            self._thread.shutdown()

    def render_html(self, html: str) -> bytes:
        return self._thread.submit(self._browser.render_html, html).result()

    def fetch(self, url: str) -> bytes | LoginGated:
        return self._thread.submit(self._browser.fetch, url).result()


def _flow_options() -> argparse.ArgumentParser:
    """The options a scheduled or one-off collection is given, as for the collect command."""
    options = argparse.ArgumentParser(add_help=False)
    add_collection_options(options)
    options.add_argument(
        "--max-concurrent",
        type=int,
        default=DEFAULT_MAX_CONCURRENT,
        help=f"how many emails to examine at once (default: {DEFAULT_MAX_CONCURRENT})",
    )
    return options


def _arguments_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="invoice-collector-flow", parents=[_flow_options()])
    parser.add_argument("month", type=CollectionMonth.parse, nargs="?")
    return parser


def _month_to_collect(arguments: Sequence[str]) -> CollectionMonth:
    given: CollectionMonth | None = _arguments_parser().parse_args(arguments).month
    if given is not None:
        return given
    # On schedule the month comes from when the run was due, not from the clock, so a
    # run that starts late still collects the month it was scheduled for.
    scheduled = flow_run.scheduled_start_time
    return month_before(scheduled)


def _collection_run_name() -> str:
    parameters: dict[str, Any] = flow_run.parameters
    return f"invoice-collection-{_month_to_collect(parameters.get('arguments') or [])}"


@flow(name="invoice-collection", flow_run_name=_collection_run_name)
def collect_with_options(arguments: list[str]) -> int:
    """Everything `invoice-collector collect` does, with the run performed as a flow.

    The arguments are those of the collect command. Without a month, the month before
    the scheduled time is collected.
    """
    args = _arguments_parser().parse_args(arguments)
    month = _month_to_collect(arguments)
    max_concurrent: int = args.max_concurrent

    def as_a_flow(
        month: CollectionMonth,
        *,
        sources: Sequence[MailSource],
        pipeline: Pipeline,
        summary_writers: Sequence[SummaryWriter],
        settings: Settings | None = None,
    ) -> RunResult:
        inputs = RunInputs(
            sources=sources, pipeline=pipeline, summary_writers=summary_writers, settings=settings
        )
        return collect_month(month, inputs, max_concurrent=max_concurrent)

    exit_code = run_collection(month, args, collector=as_a_flow, browser=ThreadConfinedBrowser)
    if exit_code != 0:
        raise RuntimeError(f"the collection for {month} did not run (exit code {exit_code})")
    return exit_code


def _command_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="invoice-collector-flow",
        description="Collect billing documents as a Prefect flow, once or every month.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    run_cmd = commands.add_parser(
        "run",
        parents=[_flow_options()],
        help="collect one month now, with no Prefect server needed",
    )
    run_cmd.add_argument("month", type=CollectionMonth.parse, help="collection month, YYYY-MM")
    commands.add_parser(
        "serve",
        parents=[_flow_options()],
        help="collect the month before on the 3rd of each month at 06:00 in India. "
        "Needs a Prefect server",
    )
    return parser


def _server_answers() -> bool:
    if not PREFECT_API_URL.value():
        return False
    with get_client(sync_client=True) as client:
        return client.api_healthcheck() is None


@contextmanager
def _local_prefect() -> Generator[None]:
    """Prefect as configured when its server answers, else a temporary one for this run."""
    if _server_answers():
        yield
        return
    with temporary_settings(
        updates={PREFECT_SERVER_EPHEMERAL_ENABLED: True}, restore_defaults={PREFECT_API_URL}
    ):
        yield


def serve_monthly(arguments: list[str]) -> None:
    """Serves the collection on its monthly schedule, until stopped."""
    collect_with_options.serve(
        name="monthly",
        schedule=MONTHLY,
        parameters={"arguments": arguments},
        description="Collects the month before, on the 3rd of each month at 06:00 in India",
        entrypoint_type=EntrypointType.MODULE_PATH,
    )


def main(
    argv: Sequence[str] | None = None, serve: Callable[[list[str]], None] = serve_monthly
) -> int:
    if not os.environ.get("INVOICE_COLLECTOR_SKIP_DOTENV"):
        load_dotenv(find_dotenv(usecwd=True))
    raw = list(sys.argv[1:] if argv is None else argv)
    args = _command_parser().parse_args(raw)
    # The flow is given the arguments as typed, so a scheduled run can read them again.
    arguments = raw[1:]
    if args.command == "serve":
        if not _server_answers():
            print(
                "A schedule needs a Prefect server to keep it. Start one with "
                "`prefect server start` and point PREFECT_API_URL at it.",
                file=sys.stderr,
            )
            return 1
        serve(arguments)
        return 0
    with _local_prefect():
        state = collect_with_options(arguments, return_state=True)
    return 0 if state.is_completed() else 1


if __name__ == "__main__":
    raise SystemExit(main())
