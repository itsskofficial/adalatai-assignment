"""What the Runs screen shows and does: past runs, and starting one from the dashboard.

Starting a run is the dashboard's way in of ADR 0010. The run itself is performed by a
Runner, which does what the command line and the schedule do; this module only decides
whether a run may start, performs it on a thread of its own so the answer comes at once,
and keeps who asked for it.

One run of a collection month goes on at a time from the dashboard. A run is seen to be
running only while this service performs it: a run started elsewhere that has not
finished may still be running there, or may have stopped, and the screen says it cannot
tell. A run started here that has not finished and is no longer performed stopped when
the service did, or when it failed.
"""

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal, Protocol

from fastapi import APIRouter, Depends, HTTPException
from fastapi import Path as PathParameter
from pydantic import BaseModel

from invoice_collector.api.run_requests import RunRequest, RunRequests, runs_of
from invoice_collector.domain import CollectionMonth, Run, StartedBy, Sync
from invoice_collector.ledger import Ledger
from invoice_collector.source_account_registry import SourceAccountRegistry, normalise

LedgerFactory = Callable[[], Ledger]
PersonDependency = Callable[..., str]
Month = Annotated[
    str,
    PathParameter(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$", description="Collection month, YYYY-MM"),
]

RunState = Literal["running", "finished", "stopped", "unfinished"]


class Runner(Protocol):
    """Performs one run started from the dashboard, as the command line and schedule do."""

    def __call__(self, month: CollectionMonth, source_account: str | None) -> None:
        """Collects the month, reading only the source account given, or every connected
        one when none is. Returns once the run is over.

        Raises when the run could not start or stopped, with the reason as its message.
        """
        ...


class RunRefused(Exception):
    """A run may not start just now. The message says why in plain words."""


@dataclass(frozen=True)
class ActiveRun:
    request_id: int
    person: str
    source_account: str | None
    requested_at: datetime


class RunStarter:
    """Starts runs on threads of their own, one at a time for each collection month."""

    def __init__(
        self,
        runner: Runner,
        requests: RunRequests,
        ledger_factory: LedgerFactory,
        now: Callable[[], datetime],
    ) -> None:
        self._runner = runner
        self._requests = requests
        self._ledger_factory = ledger_factory
        self._now = now
        self._lock = threading.Lock()
        self._active: dict[CollectionMonth, ActiveRun] = {}
        self._threads: list[threading.Thread] = []

    def active(self, month: CollectionMonth) -> ActiveRun | None:
        with self._lock:
            return self._active.get(month)

    def start(self, month: CollectionMonth, source_account: str | None, person: str) -> None:
        """Starts the run and returns at once. Raises RunRefused while one is going on."""
        with self._lock:
            going = self._active.get(month)
            if going is not None:
                raise RunRefused(
                    f"A run of {month} is already going on, started by {going.person}. "
                    "Wait for it to finish, then start another."
                )
            at = self._now()
            request_id = self._requests.asked(
                month, source_account, person, at, self._latest_run_id()
            )
            self._active[month] = ActiveRun(request_id, person, source_account, at)
            # A daemon, so stopping the service stops the run: it is then shown as stopped.
            thread = threading.Thread(
                target=self._perform,
                args=(month, source_account, request_id),
                name=f"run-{month}",
                daemon=True,
            )
            self._threads.append(thread)
        thread.start()

    def wait(self, timeout: float | None = None) -> None:
        """Waits for every run started so far to end."""
        with self._lock:
            threads = list(self._threads)
        for thread in threads:
            thread.join(timeout)

    def _latest_run_id(self) -> int:
        ledger = self._ledger_factory()
        try:
            return max((run.id for run in ledger.runs()), default=0)
        finally:
            ledger.close()

    def _perform(self, month: CollectionMonth, source_account: str | None, request_id: int) -> None:
        problem: str | None = None
        try:
            self._runner(month, source_account)
        except Exception as error:
            problem = str(error) or type(error).__name__
        finally:
            try:
                self._requests.ended(
                    request_id,
                    self._now(),
                    started_a_run=self._started_a_run(month, request_id),
                    problem=problem,
                )
            finally:
                with self._lock:
                    self._active.pop(month, None)

    def _started_a_run(self, month: CollectionMonth, request_id: int) -> bool:
        ledger = self._ledger_factory()
        try:
            runs = ledger.runs(month)
        finally:
            ledger.close()
        return request_id in runs_of(self._requests.of(month), runs)


class RunAgain(BaseModel):
    """Which source account to read again. None, or left out, runs the whole month."""

    source_account: str | None = None


class RunStarted(BaseModel):
    month: str
    source_account: str | None


class SourceAccountRead(BaseModel):
    source_account: str
    read: bool
    # Why it could not be read.
    reason: str | None
    # Its latest read for the month failed too, and it is connected, so it can be run again.
    can_run_again: bool


class ModelCost(BaseModel):
    """The calls a run made to one model, the tokens they took, and what they cost."""

    model: str
    calls: int
    input_tokens: int
    output_tokens: int
    # None when the model's price is not known.
    cost_usd: str | None


class RunView(BaseModel):
    id: int
    started_by: StartedBy
    # Who started it, for a run started from the dashboard.
    person: str | None
    # The one source account it read again, for a run of one failed source account.
    only_source_account: str | None
    state: RunState
    started_at: str
    finished_at: str | None
    duration_seconds: float | None
    # The emails the run examined, and how each ended. None until it finishes.
    emails_found: int | None
    collected: int | None
    needs_review: int | None
    skipped: int | None
    failed: int | None
    # None when it is not recorded, or unknown, which is never the same as nothing. With
    # models listed it is unknown: a model's price is not known.
    model_cost_usd: str | None
    # Each model the run called, when it metered its calls.
    models: list[ModelCost]
    source_accounts: list[SourceAccountRead]
    # Why it stopped, when it was started here and stopped with a reason.
    problem: str | None


class RunNotStarted(BaseModel):
    """A run asked for from the dashboard that ended before recording a run."""

    person: str
    only_source_account: str | None
    requested_at: str
    problem: str | None


class RunGoingOn(BaseModel):
    person: str
    only_source_account: str | None
    requested_at: str


class RunsOfMonth(BaseModel):
    month: str
    running: RunGoingOn | None
    runs: list[RunView]
    not_started: list[RunNotStarted]
    connected_source_accounts: int
    # Why a run of the month cannot be started just now, if it cannot.
    cannot_start: str | None


def _state(run: Run, request: RunRequest | None, active: ActiveRun | None) -> RunState:
    if run.finished_at is not None:
        return "finished"
    if request is not None and active is not None and active.request_id == request.id:
        return "running"
    return "stopped" if run.started_by == "dashboard" else "unfinished"


def _emails_found(run: Run) -> int | None:
    counts = (run.collected, run.needs_review, run.skipped, run.failed)
    if any(count is None for count in counts):
        return None
    return sum(count for count in counts if count is not None)


def _source_accounts(
    read: Sequence[Sync], latest: dict[str, Sync], connected: set[str]
) -> list[SourceAccountRead]:
    views: list[SourceAccountRead] = []
    for sync in read:
        now = latest.get(sync.source_account)
        still_failing = now is not None and not now.succeeded
        views.append(
            SourceAccountRead(
                source_account=sync.source_account,
                read=sync.succeeded,
                reason=sync.reason,
                can_run_again=not sync.succeeded
                and still_failing
                and normalise(sync.source_account) in connected,
            )
        )
    return views


def run_routes(
    ledger_factory: LedgerFactory,
    registry: SourceAccountRegistry,
    requests: RunRequests,
    starter: RunStarter | None,
    signed_in_person: PersonDependency,
) -> APIRouter:
    router = APIRouter(prefix="/months/{month}/runs")

    def cannot_start(month: CollectionMonth, connected: Sequence[str]) -> str | None:
        if starter is None:
            return "Starting a run from the dashboard is not set up on this service."
        going = starter.active(month)
        if going is not None:
            return f"A run of {month} is already going on, started by {going.person}."
        if not connected:
            return (
                "No source account is connected. Connect one on the Source accounts screen first."
            )
        return None

    @router.get("")
    def runs(month: Month) -> RunsOfMonth:  # pyright: ignore[reportUnusedFunction]
        collection_month = CollectionMonth.parse(month)
        # Asked before the ledger is read. A run that ends in between is then still shown
        # as running, which the next reading puts right. Asked after, a run could be read
        # as unfinished and then found to be no longer performed, and be shown as stopped.
        active = starter.active(collection_month) if starter is not None else None
        ledger = ledger_factory()
        try:
            recorded = ledger.runs(collection_month)
            latest = {s.source_account: s for s in ledger.syncs(collection_month)}
        finally:
            ledger.close()
        asked = requests.of(collection_month)
        by_id = {request.id: request for request in asked}
        request_of = {
            run.id: by_id[request_id] for request_id, run in runs_of(asked, recorded).items()
        }
        connected = registry.addresses()
        views = [
            RunView(
                id=run.id,
                started_by=run.started_by,
                person=request.person if request is not None else None,
                only_source_account=request.source_account if request is not None else None,
                state=_state(run, request, active),
                started_at=run.started_at.isoformat(),
                finished_at=run.finished_at.isoformat() if run.finished_at else None,
                duration_seconds=(
                    run.duration.total_seconds() if run.duration is not None else None
                ),
                emails_found=_emails_found(run),
                collected=run.collected,
                needs_review=run.needs_review,
                skipped=run.skipped,
                failed=run.failed,
                model_cost_usd=(
                    str(run.model_cost_usd) if run.model_cost_usd is not None else None
                ),
                models=[
                    ModelCost(
                        model=m.model,
                        calls=m.calls,
                        input_tokens=m.input_tokens,
                        output_tokens=m.output_tokens,
                        cost_usd=str(m.cost_usd) if m.cost_usd is not None else None,
                    )
                    for m in run.models
                ],
                source_accounts=_source_accounts(run.source_accounts, latest, set(connected)),
                problem=request.problem if request is not None else None,
            )
            for run in recorded
            for request in [request_of.get(run.id)]
        ]
        not_started = [
            RunNotStarted(
                person=request.person,
                only_source_account=request.source_account,
                requested_at=request.requested_at.isoformat(),
                problem=request.problem,
            )
            for request in reversed(asked)
            if request.started_a_run is False
        ]
        return RunsOfMonth(
            month=month,
            running=(
                RunGoingOn(
                    person=active.person,
                    only_source_account=active.source_account,
                    requested_at=active.requested_at.isoformat(),
                )
                if active is not None
                else None
            ),
            runs=views,
            not_started=not_started,
            connected_source_accounts=len(connected),
            cannot_start=cannot_start(collection_month, connected),
        )

    @router.post("", status_code=202)
    def run_again(  # pyright: ignore[reportUnusedFunction]
        month: Month,
        asked: RunAgain,
        person: Annotated[str, Depends(signed_in_person)],
    ) -> RunStarted:
        collection_month = CollectionMonth.parse(month)
        connected = registry.addresses()
        refusal = cannot_start(collection_month, connected)
        if refusal is not None:
            status = 503 if starter is None else 409
            raise HTTPException(status_code=status, detail=refusal)
        assert starter is not None
        account = asked.source_account
        if account is not None:
            account = normalise(account)
            if account not in connected:
                raise HTTPException(
                    status_code=409,
                    detail=f"{account} is not a connected source account. Connect it on the "
                    "Source accounts screen, then run the month.",
                )
            ledger = ledger_factory()
            try:
                latest = {normalise(s.source_account): s for s in ledger.syncs(collection_month)}
            finally:
                ledger.close()
            sync = latest.get(account)
            if sync is None or sync.succeeded:
                raise HTTPException(
                    status_code=409,
                    detail=f"{account} did not fail when {month} was last run, so there is "
                    "nothing to run again for it alone. Run the whole month instead.",
                )
        try:
            starter.start(collection_month, account, person)
        except RunRefused as refused:
            raise HTTPException(status_code=409, detail=str(refused)) from None
        return RunStarted(month=month, source_account=account)

    return router
