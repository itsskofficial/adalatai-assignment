"""The runner's HTTP API, which the app calls over the private network between them.

Every route but the health route needs the shared secret, sent by the app as a bearer
token, so reaching the runner's port is not enough to start a run. The health route says
only whether the runner is up, whether a run is going on and when the next scheduled run
is due, for whatever watches the container.
"""

import hmac
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from invoice_collector.domain import CollectionMonth, StartedBy
from invoice_collector.run_starter import ActiveRun, RunRefused
from invoice_collector.runner.service import RunnerService
from invoice_collector.schedule import Due

MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"


class StartRun(BaseModel):
    month: Annotated[str, Field(pattern=MONTH_PATTERN)]
    # Only this source account is read. None reads every connected one.
    source_account: str | None = None
    # Who asked for it in the dashboard.
    person: Annotated[str, Field(min_length=1)]


class RunStarted(BaseModel):
    month: str
    source_account: str | None


class RunGoingOn(BaseModel):
    collection_month: str
    started_by: StartedBy
    person: str | None
    source_account: str | None
    requested_at: str
    after_run_id: int
    request_id: int | None


class RunsGoingOn(BaseModel):
    runs: list[RunGoingOn]


class ScheduledRun(BaseModel):
    due_at: str
    collection_month: str


class Scheduled(BaseModel):
    next_scheduled_run: ScheduledRun | None


class RunInProgress(BaseModel):
    collection_month: str
    started_by: StartedBy


class Health(BaseModel):
    up: bool
    running: bool
    runs_going_on: list[RunInProgress]
    next_scheduled_run: ScheduledRun | None


def scheduled_run(due: Due | None) -> ScheduledRun | None:
    if due is None:
        return None
    return ScheduledRun(due_at=due.at.isoformat(), collection_month=str(due.collection_month))


def _going_on(run: ActiveRun) -> RunGoingOn:
    return RunGoingOn(
        collection_month=str(run.collection_month),
        started_by=run.started_by,
        person=run.person,
        source_account=run.source_account,
        requested_at=run.requested_at.isoformat(),
        after_run_id=run.after_run_id,
        request_id=run.request_id,
    )


def runner_app(service: RunnerService, secret: str) -> FastAPI:
    if not secret.strip():
        raise ValueError("the runner needs a shared secret")
    expected = f"Bearer {secret}".encode()

    def from_the_app(authorization: Annotated[str, Header()] = "") -> None:
        if not hmac.compare_digest(authorization.encode(), expected):
            raise HTTPException(status_code=401, detail="The shared secret is missing or wrong")

    app = FastAPI(title="Invoice Collection runner", docs_url=None, redoc_url=None)
    app.state.service = service
    asked = [Depends(from_the_app)]

    @app.get("/health")
    def health() -> Health:  # pyright: ignore[reportUnusedFunction]
        going = service.starter.going_on()
        return Health(
            up=True,
            running=bool(going),
            runs_going_on=[
                RunInProgress(collection_month=str(run.collection_month), started_by=run.started_by)
                for run in going
            ],
            next_scheduled_run=scheduled_run(service.next_due),
        )

    @app.get("/runs", dependencies=asked)
    def runs() -> RunsGoingOn:  # pyright: ignore[reportUnusedFunction]
        return RunsGoingOn(runs=[_going_on(run) for run in service.starter.going_on()])

    @app.post("/runs", status_code=202, dependencies=asked)
    def start(asked: StartRun) -> RunStarted:  # pyright: ignore[reportUnusedFunction]
        try:
            service.starter.start(
                CollectionMonth.parse(asked.month), asked.source_account, asked.person
            )
        except RunRefused as refused:
            raise HTTPException(status_code=409, detail=str(refused)) from None
        return RunStarted(month=asked.month, source_account=asked.source_account)

    @app.post("/schedule", dependencies=asked)
    def schedule_changed() -> Scheduled:  # pyright: ignore[reportUnusedFunction]
        return Scheduled(next_scheduled_run=scheduled_run(service.schedule_changed()))

    return app
