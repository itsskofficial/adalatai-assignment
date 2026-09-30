"""What the Runs screen shows and does: past runs, and starting one from the dashboard.

Starting a run is the dashboard's way in of ADR 0010. The run itself is performed by the
runner service when the dashboard has one, or on a thread of this service when it does
not (run_starter.py); this module only decides whether a run may start and asks for it.

One run of a collection month goes on at a time. A run is seen to be running only while
whatever performs it says so: a run started another way that has not finished may still
be running elsewhere, or may have stopped, and the screen says it cannot tell. A run
started a way the performer handles that has not finished and is no longer performed
stopped when the performer did, or when it failed. When the runner cannot be reached,
nothing can be told of its runs, and the screen says so.
"""

from collections.abc import Callable, Sequence
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi import Path as PathParameter
from pydantic import BaseModel

from invoice_collector.domain import CollectionMonth, Run, StartedBy, Sync
from invoice_collector.ledger import Ledger
from invoice_collector.run_requests import RunRequests, runs_of
from invoice_collector.run_starter import ActiveRun, RunnerUnreachable, RunRefused, Runs
from invoice_collector.source_account_registry import SourceAccountRegistry, normalise

LedgerFactory = Callable[[], Ledger]
PersonDependency = Callable[..., str]
Month = Annotated[
    str,
    PathParameter(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$", description="Collection month, YYYY-MM"),
]

RunState = Literal["running", "finished", "stopped", "unfinished"]


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
    started_by: StartedBy
    # Who asked for it, for a run started from the dashboard.
    person: str | None
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
    # Why the runner service could not be asked what goes on, when it could not. Its
    # unfinished runs are then shown as not finished, since nothing can be told of them.
    runner_problem: str | None = None


def _state(run: Run, running: Run | None, performs: frozenset[StartedBy], known: bool) -> RunState:
    """Running while the performer says it performs it; stopped when it performs runs
    started that way and not this one; not finished when that cannot be told."""
    if run.finished_at is not None:
        return "finished"
    if running is not None and running.id == run.id:
        return "running"
    return "stopped" if known and run.started_by in performs else "unfinished"


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
    starter: Runs | None,
    signed_in_person: PersonDependency,
) -> APIRouter:
    router = APIRouter(prefix="/months/{month}/runs")

    def going_on(month: CollectionMonth) -> tuple[ActiveRun | None, str | None]:
        """The run of the month going on, and why that could not be asked, if it could not."""
        if starter is None:
            return None, None
        try:
            return starter.active(month), None
        except RunnerUnreachable as unreachable:
            return None, str(unreachable)

    def cannot_start(
        month: CollectionMonth,
        connected: Sequence[str],
        going: ActiveRun | None,
        problem: str | None,
    ) -> str | None:
        if starter is None:
            return "Starting a run from the dashboard is not set up on this service."
        if problem is not None:
            return problem
        if going is not None:
            who = going.person or "the schedule"
            return f"A run of {month} is already going on, started by {who}."
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
        active, problem = going_on(collection_month)
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
        running = active.run_in(recorded) if active is not None else None
        performs = starter.performs if starter is not None else frozenset[StartedBy]()
        views = [
            RunView(
                id=run.id,
                started_by=run.started_by,
                person=request.person if request is not None else None,
                only_source_account=request.source_account if request is not None else None,
                state=_state(run, running, performs, known=problem is None),
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
                    started_by=active.started_by,
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
            cannot_start=cannot_start(collection_month, connected, active, problem),
            runner_problem=problem,
        )

    @router.post("", status_code=202)
    def run_again(  # pyright: ignore[reportUnusedFunction]
        month: Month,
        asked: RunAgain,
        person: Annotated[str, Depends(signed_in_person)],
    ) -> RunStarted:
        collection_month = CollectionMonth.parse(month)
        connected = registry.addresses()
        active, problem = going_on(collection_month)
        refusal = cannot_start(collection_month, connected, active, problem)
        if refusal is not None:
            status = 503 if starter is None or problem is not None else 409
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
        except RunnerUnreachable as unreachable:
            raise HTTPException(
                status_code=503, detail=f"{unreachable} The run was not started."
            ) from None
        return RunStarted(month=month, source_account=account)

    return router
