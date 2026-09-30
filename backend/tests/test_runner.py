"""The runner service, through its HTTP API, with a fake run function, a clock that is moved
by hand and a timer that fires when the test says. Nothing sleeps.

The run function stands in for cli.run_collection, which the runner is given in production:
it records a run in the ledger as that does, with how it was started.
"""

import argparse
import threading
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest
from browser.harness import OFFLINE, run_over_samples
from fastapi import FastAPI
from fastapi.testclient import TestClient

from invoice_collector.api.collection_runner import CollectionRunner
from invoice_collector.collection_settings import CollectionSettings, SettingsStore
from invoice_collector.domain import CollectionMonth, EmailState, StartedBy
from invoice_collector.ledger import Ledger
from invoice_collector.run_requests import RunRequests
from invoice_collector.run_starter import RunStarter
from invoice_collector.runner import serve
from invoice_collector.runner.http_api import runner_app
from invoice_collector.runner.service import RunnerService
from invoice_collector.schedule import Schedule

SECRET = "a-secret-only-for-tests"
ASKED = {"Authorization": f"Bearer {SECRET}"}
FINANCE = "finance@nyayalabs.example"
ADMIN = "admin@nyayalabs.example"
AUGUST = CollectionMonth(2026, 8)
SEPTEMBER = CollectionMonth(2026, 9)
# The schedule the tests keep: the 3rd at 06:00 in India, which is 00:30 UTC.
THIRD = Schedule(enabled=True, day=3, at=time(6, 0), time_zone="Asia/Kolkata")
DUE_IN_SEPTEMBER = datetime(2026, 9, 3, 0, 30, tzinfo=UTC)
DUE_IN_OCTOBER = datetime(2026, 10, 3, 0, 30, tzinfo=UTC)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@dataclass
class FakeTimer:
    seconds: float
    action: Callable[[], None]
    cancelled: bool = False

    def cancel(self) -> None:
        self.cancelled = True


@dataclass
class Timers:
    """Keeps every timer set, for the test to fire."""

    set: list[FakeTimer] = field(default_factory=list[FakeTimer])

    def __call__(self, seconds: float, action: Callable[[], None]) -> FakeTimer:
        timer = FakeTimer(seconds, action)
        self.set.append(timer)
        return timer

    @property
    def waiting(self) -> list[FakeTimer]:
        return [timer for timer in self.set if not timer.cancelled]

    def fire(self) -> None:
        [timer] = self.waiting
        timer.cancelled = True
        timer.action()


class FakeRun:
    """Records a run in the ledger as cli.run_collection does, reading no mail.

    A run waits for release when held, so a test can look at it while it goes on.
    """

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.asked: list[tuple[CollectionMonth, StartedBy, list[str]]] = []
        self.release = threading.Event()
        self.release.set()
        self.started = threading.Event()

    def __call__(
        self, month: CollectionMonth, args: argparse.Namespace, *, started_by: StartedBy
    ) -> int:
        self.asked.append((month, started_by, list(args.account) or ["--connected-accounts"]))
        ledger = Ledger(args.out / "ledger.sqlite")
        try:
            run_id = ledger.start_run(month, started_by, self.clock())
            self.started.set()
            self.release.wait(10)
            ledger.finish_run(run_id, self.clock(), Counter({EmailState.COLLECTED: 1}))
        finally:
            ledger.close()
        return 0


@dataclass
class Runner:
    """The runner service over a ledger, with its HTTP API."""

    ledger_path: Path
    clock: Clock
    timers: Timers
    run: FakeRun
    service: RunnerService
    api: TestClient

    def runs(self, month: CollectionMonth = AUGUST) -> list[tuple[StartedBy, bool]]:
        ledger = Ledger(self.ledger_path)
        try:
            return [(r.started_by, r.finished_at is not None) for r in ledger.runs(month)]
        finally:
            ledger.close()

    def keep(self, schedule: Schedule, at: datetime) -> None:
        SettingsStore(self.ledger_path).change(CollectionSettings(schedule), ADMIN, at)


def a_runner(tmp_path: Path, now: datetime) -> Runner:
    ledger_path = tmp_path / "out" / "ledger.sqlite"
    Ledger(ledger_path).close()
    clock, timers = Clock(now), Timers()
    run = FakeRun(clock)

    def ledger() -> Ledger:
        return Ledger(ledger_path)

    starter = RunStarter(
        CollectionRunner(ledger_path, tmp_path / "tokens", run=run),
        RunRequests(ledger_path),
        ledger,
        clock,
        performs=frozenset({"dashboard", "schedule"}),
    )
    service = RunnerService(starter, SettingsStore(ledger_path), ledger, clock, timers)
    api = TestClient(runner_app(service, SECRET))
    return Runner(ledger_path, clock, timers, run, service, api)


@pytest.fixture
def runner(tmp_path: Path) -> Iterator[Runner]:
    made = a_runner(tmp_path, datetime(2026, 9, 1, tzinfo=UTC))
    yield made
    made.run.release.set()
    made.service.starter.wait(10)


# Asked by the app


def test_a_run_is_started_at_once_and_performed_in_the_background(runner: Runner) -> None:
    runner.run.release.clear()

    answer = runner.api.post("/runs", json={"month": "2026-08", "person": FINANCE}, headers=ASKED)

    assert answer.status_code == 202
    assert runner.run.started.wait(10)
    [going] = runner.api.get("/runs", headers=ASKED).json()["runs"]
    assert (going["collection_month"], going["started_by"], going["person"]) == (
        "2026-08",
        "dashboard",
        FINANCE,
    )
    assert runner.runs() == [("dashboard", False)]

    runner.run.release.set()
    runner.service.starter.wait(10)

    assert runner.runs() == [("dashboard", True)]
    assert runner.api.get("/runs", headers=ASKED).json()["runs"] == []
    # Who asked is kept, as the dashboard keeps it.
    [request] = RunRequests(runner.ledger_path).of(AUGUST)
    assert (request.person, request.started_a_run) == (FINANCE, True)


def test_the_run_is_the_collect_commands_for_every_connected_account_or_one(
    runner: Runner,
) -> None:
    runner.api.post("/runs", json={"month": "2026-08", "person": FINANCE}, headers=ASKED)
    runner.service.starter.wait(10)
    runner.api.post(
        "/runs",
        json={"month": "2026-08", "person": FINANCE, "source_account": "ops@nyayalabs.example"},
        headers=ASKED,
    )
    runner.service.starter.wait(10)

    assert runner.run.asked == [
        (AUGUST, "dashboard", ["--connected-accounts"]),
        (AUGUST, "dashboard", ["ops@nyayalabs.example"]),
    ]


def test_a_second_run_of_a_month_is_refused_while_one_goes_on(runner: Runner) -> None:
    runner.run.release.clear()
    runner.api.post("/runs", json={"month": "2026-08", "person": FINANCE}, headers=ASKED)
    assert runner.run.started.wait(10)

    second = runner.api.post("/runs", json={"month": "2026-08", "person": ADMIN}, headers=ASKED)
    other_month = runner.api.post(
        "/runs", json={"month": "2026-07", "person": ADMIN}, headers=ASKED
    )

    assert second.status_code == 409
    assert second.json()["detail"] == (
        f"A run of 2026-08 is already going on, started by {FINANCE}. "
        "Wait for it to finish, then start another."
    )
    assert other_month.status_code == 202


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer not-the-secret"}])
def test_nothing_is_started_or_told_without_the_shared_secret(
    runner: Runner, headers: dict[str, str]
) -> None:
    started = runner.api.post(
        "/runs", json={"month": "2026-08", "person": FINANCE}, headers=headers
    )
    listed = runner.api.get("/runs", headers=headers)
    changed = runner.api.post("/schedule", headers=headers)

    assert [started.status_code, listed.status_code, changed.status_code] == [401, 401, 401]
    assert runner.run.asked == []


def test_the_runner_refuses_an_empty_secret(runner: Runner) -> None:
    with pytest.raises(ValueError, match="shared secret"):
        runner_app(runner.service, "  ")


def test_the_health_route_says_whether_a_run_goes_on_and_when_the_next_is_due(
    runner: Runner,
) -> None:
    runner.keep(THIRD, runner.clock.now)
    runner.service.start()
    runner.run.release.clear()

    idle = runner.api.get("/health").json()
    runner.api.post("/runs", json={"month": "2026-08", "person": FINANCE}, headers=ASKED)
    assert runner.run.started.wait(10)
    busy = runner.api.get("/health").json()

    assert idle == {
        "up": True,
        "running": False,
        "runs_going_on": [],
        "next_scheduled_run": {
            "due_at": DUE_IN_SEPTEMBER.isoformat(),
            "collection_month": "2026-08",
        },
    }
    assert busy["running"] is True
    assert busy["runs_going_on"] == [{"collection_month": "2026-08", "started_by": "dashboard"}]
    # Who asked is not told to anyone without the secret.
    assert FINANCE not in str(busy)


# The schedule


def test_the_timer_is_set_for_the_moment_the_next_run_is_due(runner: Runner) -> None:
    runner.keep(THIRD, runner.clock.now)

    runner.service.start()

    [timer] = runner.timers.waiting
    assert timer.seconds == (DUE_IN_SEPTEMBER - runner.clock.now).total_seconds()
    assert runner.run.asked == []


def test_when_the_run_is_due_the_month_that_has_just_ended_is_collected(runner: Runner) -> None:
    runner.keep(THIRD, runner.clock.now)
    runner.service.start()

    runner.clock.now = DUE_IN_SEPTEMBER
    runner.timers.fire()
    runner.service.starter.wait(10)

    assert runner.run.asked == [(AUGUST, "schedule", ["--connected-accounts"])]
    assert runner.runs() == [("schedule", True)]
    # And the timer is set again for the next month's.
    [timer] = runner.timers.waiting
    assert timer.seconds == (DUE_IN_OCTOBER - DUE_IN_SEPTEMBER).total_seconds()
    assert runner.service.next_due is not None
    assert runner.service.next_due.collection_month == SEPTEMBER


def test_with_the_schedule_off_no_timer_is_set(runner: Runner) -> None:
    runner.service.start()

    assert runner.timers.waiting == []
    assert runner.service.next_due is None
    assert runner.api.get("/health").json()["next_scheduled_run"] is None


def test_when_told_the_schedule_changed_the_moment_is_worked_out_again(runner: Runner) -> None:
    runner.keep(THIRD, runner.clock.now)
    runner.service.start()
    [first] = runner.timers.waiting
    fifth = Schedule(enabled=True, day=5, at=time(6, 0), time_zone="Asia/Kolkata")
    runner.keep(fifth, runner.clock.now)

    answer = runner.api.post("/schedule", headers=ASKED)

    assert answer.json() == {
        "next_scheduled_run": {
            "due_at": datetime(2026, 9, 5, 0, 30, tzinfo=UTC).isoformat(),
            "collection_month": "2026-08",
        }
    }
    assert first.cancelled
    [second] = runner.timers.waiting
    assert (
        second.seconds
        == (datetime(2026, 9, 5, 0, 30, tzinfo=UTC) - runner.clock.now).total_seconds()
    )


def test_a_timer_set_before_the_schedule_changed_does_nothing(runner: Runner) -> None:
    runner.keep(THIRD, runner.clock.now)
    runner.service.start()
    [stale] = runner.timers.waiting
    runner.keep(Schedule(enabled=False), runner.clock.now)
    runner.service.schedule_changed()

    runner.clock.now = DUE_IN_SEPTEMBER
    stale.action()

    assert runner.run.asked == []
    assert runner.timers.waiting == []


def test_a_run_moved_earlier_while_the_runner_could_not_be_told_is_caught_up(
    runner: Runner,
) -> None:
    runner.keep(THIRD, runner.clock.now)
    runner.service.start()
    # Moved to the 2nd, and the app could not tell the runner: its timer for the 3rd stays.
    runner.keep(
        Schedule(enabled=True, day=2, at=time(6, 0), time_zone="Asia/Kolkata"), runner.clock.now
    )

    runner.clock.now = DUE_IN_SEPTEMBER
    runner.timers.fire()
    runner.service.starter.wait(10)

    assert runner.run.asked == [(AUGUST, "schedule", ["--connected-accounts"])]
    [timer] = runner.timers.waiting
    assert (
        timer.seconds
        == (datetime(2026, 10, 2, 0, 30, tzinfo=UTC) - DUE_IN_SEPTEMBER).total_seconds()
    )


def test_a_timer_that_fires_early_is_set_again_for_what_is_left(runner: Runner) -> None:
    runner.keep(THIRD, runner.clock.now)
    runner.service.start()

    runner.clock.now = DUE_IN_SEPTEMBER - timedelta(seconds=2)
    runner.timers.fire()

    assert runner.run.asked == []
    [again] = runner.timers.waiting
    assert again.seconds == 2


def test_a_scheduled_run_due_while_that_month_is_being_run_does_not_start_a_second(
    runner: Runner, caplog: pytest.LogCaptureFixture
) -> None:
    runner.keep(THIRD, runner.clock.now)
    runner.service.start()
    runner.run.release.clear()
    runner.api.post("/runs", json={"month": "2026-08", "person": FINANCE}, headers=ASKED)
    assert runner.run.started.wait(10)

    runner.clock.now = DUE_IN_SEPTEMBER
    runner.timers.fire()

    assert [started_by for _, started_by, _ in runner.run.asked] == ["dashboard"]
    assert "The scheduled run of 2026-08 did not start" in caplog.text
    assert len(runner.timers.waiting) == 1


def test_the_timer_is_set_again_even_when_the_scheduled_run_could_not_start(
    runner: Runner, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner.keep(THIRD, runner.clock.now)
    runner.service.start()

    def cannot_start(month: CollectionMonth) -> int:
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(runner.service.starter, "start_scheduled", cannot_start)
    runner.clock.now = DUE_IN_SEPTEMBER
    runner.timers.fire()

    assert "The scheduled run of 2026-08 did not start" in caplog.text
    assert "can't start new thread" in caplog.text
    [timer] = runner.timers.waiting
    assert timer.seconds == (DUE_IN_OCTOBER - DUE_IN_SEPTEMBER).total_seconds()


def test_a_run_asked_for_while_the_scheduled_one_goes_on_is_refused(runner: Runner) -> None:
    runner.keep(THIRD, runner.clock.now)
    runner.service.start()
    runner.run.release.clear()
    runner.clock.now = DUE_IN_SEPTEMBER
    runner.timers.fire()
    assert runner.run.started.wait(10)

    refused = runner.api.post("/runs", json={"month": "2026-08", "person": FINANCE}, headers=ASKED)

    assert refused.status_code == 409
    assert "started by the schedule" in refused.json()["detail"]
    [going] = runner.api.get("/runs", headers=ASKED).json()["runs"]
    assert (going["started_by"], going["person"], going["request_id"]) == ("schedule", None, None)


# A run missed while the runner was not running


def test_a_run_due_while_the_runner_was_not_running_is_performed_when_it_starts(
    tmp_path: Path,
) -> None:
    runner = a_runner(tmp_path, datetime(2026, 9, 29, 10, 0, tzinfo=UTC))
    runner.keep(THIRD, datetime(2026, 8, 20, tzinfo=UTC))

    runner.service.start()
    runner.service.starter.wait(10)

    assert runner.run.asked == [(AUGUST, "schedule", ["--connected-accounts"])]
    [timer] = runner.timers.waiting
    assert timer.seconds == (DUE_IN_OCTOBER - runner.clock.now).total_seconds()


def test_a_run_that_finished_after_it_was_due_is_not_performed_again(tmp_path: Path) -> None:
    runner = a_runner(tmp_path, datetime(2026, 9, 29, 10, 0, tzinfo=UTC))
    runner.keep(THIRD, datetime(2026, 8, 20, tzinfo=UTC))
    ledger = Ledger(runner.ledger_path)
    run_id = ledger.start_run(AUGUST, "dashboard", datetime(2026, 9, 4, tzinfo=UTC))
    ledger.finish_run(run_id, datetime(2026, 9, 4, 1, tzinfo=UTC), Counter())
    ledger.close()

    runner.service.start()

    assert runner.run.asked == []


def test_a_run_that_did_not_finish_after_it_was_due_is_performed_again(tmp_path: Path) -> None:
    runner = a_runner(tmp_path, datetime(2026, 9, 29, 10, 0, tzinfo=UTC))
    runner.keep(THIRD, datetime(2026, 8, 20, tzinfo=UTC))
    ledger = Ledger(runner.ledger_path)
    ledger.start_run(AUGUST, "schedule", DUE_IN_SEPTEMBER)  # the runner stopped during it
    ledger.close()

    runner.service.start()
    runner.service.starter.wait(10)

    assert runner.run.asked == [(AUGUST, "schedule", ["--connected-accounts"])]


def test_a_run_due_before_the_schedule_was_set_is_not_missed(tmp_path: Path) -> None:
    runner = a_runner(tmp_path, datetime(2026, 9, 29, 10, 0, tzinfo=UTC))
    runner.keep(THIRD, datetime(2026, 9, 20, tzinfo=UTC))

    runner.service.start()

    assert runner.run.asked == []
    assert len(runner.timers.waiting) == 1


# The command


@dataclass
class Served:
    apps: list[tuple[FastAPI, str, int]] = field(default_factory=list[tuple[FastAPI, str, int]])

    def __call__(self, app: FastAPI, host: str, port: int) -> None:
        self.apps.append((app, host, port))


def command(
    tmp_path: Path, environment: dict[str, str], *extra: str
) -> tuple[int, Served, list[Any]]:
    served = Served()
    timers = Timers()
    code = serve.main(
        ["--ledger", str(tmp_path / "out" / "ledger.sqlite"), *extra],
        environment=environment,
        serve=served,
        run=FakeRun(Clock(datetime(2026, 9, 1, tzinfo=UTC))),
        clock=Clock(datetime(2026, 9, 1, tzinfo=UTC)),
        timer=timers,
    )
    return code, served, timers.set


def test_the_command_listens_on_localhost_by_default(tmp_path: Path) -> None:
    code, served, _ = command(tmp_path, {serve.SECRET_VARIABLE: SECRET})

    assert code == 0
    [(_, host, port)] = served.apps
    assert (host, port) == ("127.0.0.1", 8001)


def test_the_command_listens_where_it_is_told(tmp_path: Path) -> None:
    environment = {
        serve.SECRET_VARIABLE: SECRET,
        serve.HOST_VARIABLE: "0.0.0.0",
        serve.PORT_VARIABLE: "9100",
    }

    code, served, _ = command(tmp_path, environment)

    assert code == 0
    [(_, host, port)] = served.apps
    assert (host, port) == ("0.0.0.0", 9100)


def test_the_command_refuses_to_start_without_a_secret(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, served, _ = command(tmp_path, {serve.SECRET_VARIABLE: " "})

    assert code == 2
    assert served.apps == []
    assert "INVOICE_COLLECTOR_RUNNER_SECRET is not set" in capsys.readouterr().err


def test_the_command_refuses_run_options_it_sets_itself(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, served, _ = command(
        tmp_path, {serve.SECRET_VARIABLE: SECRET}, "--run-options=--out elsewhere"
    )

    assert code == 2
    assert served.apps == []
    assert "--out cannot be among the run options" in capsys.readouterr().err


def test_the_command_sets_the_timer_from_the_ledger_when_it_starts(tmp_path: Path) -> None:
    ledger_path = tmp_path / "out" / "ledger.sqlite"
    SettingsStore(ledger_path).change(
        CollectionSettings(THIRD), ADMIN, datetime(2026, 8, 1, tzinfo=UTC)
    )

    code, _, timers = command(tmp_path, {serve.SECRET_VARIABLE: SECRET})

    assert code == 0
    [timer] = timers
    assert timer.seconds == (DUE_IN_SEPTEMBER - datetime(2026, 9, 1, tzinfo=UTC)).total_seconds()
    # Stopped when serving ends.
    assert timer.cancelled


def test_the_command_performs_the_collect_commands_run(tmp_path: Path) -> None:
    served = Served()
    code = serve.main(
        [
            "--ledger",
            str(tmp_path / "out" / "ledger.sqlite"),
            "--run-options",
            " ".join(OFFLINE),
        ],
        environment={serve.SECRET_VARIABLE: SECRET},
        serve=served,
        run=run_over_samples,
        timer=Timers(),
    )
    [(app, _, _)] = served.apps
    api = TestClient(app)

    answer = api.post("/runs", json={"month": "2026-08", "person": FINANCE}, headers=ASKED)
    service: RunnerService = app.state.service
    service.starter.wait(10)

    assert code == 0 and answer.status_code == 202
    ledger = Ledger(tmp_path / "out" / "ledger.sqlite")
    try:
        [run] = ledger.runs(AUGUST)
        documents = ledger.documents(AUGUST)
    finally:
        ledger.close()
    assert (run.started_by, run.finished_at is not None) == ("dashboard", True)
    assert documents
    assert (tmp_path / "out" / "2026-08_summary.csv").is_file()


def test_a_timer_for_a_moment_the_settings_no_longer_give_runs_nothing(runner: Runner) -> None:
    runner.keep(THIRD, runner.clock.now)
    runner.service.start()
    # Changed while the app could not tell the runner.
    fifth = Schedule(enabled=True, day=5, at=time(6, 0), time_zone="Asia/Kolkata")
    runner.keep(fifth, runner.clock.now)

    runner.clock.now = DUE_IN_SEPTEMBER
    runner.timers.fire()

    assert runner.run.asked == []
    [timer] = runner.timers.waiting
    fifth_due = datetime(2026, 9, 5, 0, 30, tzinfo=UTC)
    assert timer.seconds == (fifth_due - DUE_IN_SEPTEMBER).total_seconds()
