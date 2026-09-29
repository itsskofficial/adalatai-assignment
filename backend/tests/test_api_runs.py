"""The Runs screen's API, called the way the dashboard calls it.

The dashboard is given a fake runner, which records a run in the ledger as a real one does.
test_collection_runner.py gives it the real one.
"""

import threading
import time
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_api import AUGUST, ENGINEERING, FINANCE, JULY
from test_api import sign_in as sign_in_to_dashboard

from invoice_collector.api.app import create_app
from invoice_collector.api.identity import FakeIdentityVerifier
from invoice_collector.api.people import People
from invoice_collector.api.run_requests import RunRequests
from invoice_collector.api.runs import Runner
from invoice_collector.api.settings import Settings
from invoice_collector.domain import CollectionMonth, EmailState
from invoice_collector.ledger import Ledger
from invoice_collector.source_account_registry import SourceAccountRegistry

NOW = datetime(2026, 9, 29, 10, 30, tzinfo=UTC)
MEMBER = "member@nyayalabs.example"
RUNS = "/api/months/2026-08/runs"


class Clock:
    """Moves on a minute each time it is read."""

    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        at = self.now
        self.now += timedelta(minutes=1)
        return at


class FakeRunner:
    """Records a run in the ledger as the real runner does, without reading any mail.

    Each source account in failing cannot be read. A run waits for release when held.
    """

    def __init__(self, ledger_path: Path, clock: Clock) -> None:
        self.ledger_path = ledger_path
        self.clock = clock
        self.asked: list[tuple[CollectionMonth, str | None]] = []
        self.failing: dict[str, str] = {}
        self.release = threading.Event()
        self.release.set()
        self.started = threading.Event()
        self.stops_with: str | None = None
        self.refuses_with: str | None = None

    def __call__(self, month: CollectionMonth, source_account: str | None) -> None:
        self.asked.append((month, source_account))
        if self.refuses_with is not None:
            self.started.set()
            raise RuntimeError(self.refuses_with)
        ledger = Ledger(self.ledger_path)
        try:
            run_id = ledger.start_run(month, "dashboard", self.clock())
            accounts = (
                [source_account]
                if source_account
                else SourceAccountRegistry(self.ledger_path).addresses()
            )
            for account in accounts:
                ledger.record_sync(month, account, self.failing.get(account), run_id)
            self.started.set()
            self.release.wait(5)
            if self.stops_with is not None:
                raise RuntimeError(self.stops_with)
            states = Counter({EmailState.COLLECTED: 2, EmailState.SKIPPED: 1})
            ledger.finish_run(run_id, self.clock(), states)
        finally:
            ledger.close()


@pytest.fixture
def ledger_path(tmp_path: Path) -> Path:
    return tmp_path / "out" / "ledger.sqlite"


@pytest.fixture
def ledger(ledger_path: Path) -> Iterator[Ledger]:
    ledger = Ledger(ledger_path)
    yield ledger
    ledger.close()


@pytest.fixture
def settings(ledger_path: Path, tmp_path: Path) -> Settings:
    return Settings(
        session_secret="a-secret-only-for-tests",
        allowlist=frozenset({FINANCE}),
        ledger_path=ledger_path,
        token_dir=tmp_path / "tokens",
    )


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def runner(ledger_path: Path, clock: Clock) -> FakeRunner:
    return FakeRunner(ledger_path, clock)


def open_dashboard(
    settings: Settings, runner: Runner | None, clock: Clock, code: str = "code-finance"
) -> TestClient:
    verifier = FakeIdentityVerifier({"code-finance": FINANCE, "code-member": MEMBER})
    app = create_app(
        settings, lambda: Ledger(settings.ledger_path), verifier, runner=runner, now=clock
    )
    client = TestClient(app, base_url="http://localhost:8000", follow_redirects=False)
    sign_in_to_dashboard(client, code)
    return client


@pytest.fixture
def dashboard(settings: Settings, runner: FakeRunner, clock: Clock) -> Iterator[TestClient]:
    with open_dashboard(settings, runner, clock) as client:
        yield client


def connect(settings: Settings, *addresses: str) -> None:
    registry = SourceAccountRegistry(settings.ledger_path)
    for address in addresses:
        registry.signed_in(address, "connected", FINANCE, NOW, signed_in_at=None, fingerprint=None)


def runs_when_done(dashboard: TestClient, path: str = RUNS) -> dict[str, Any]:
    """The Runs screen's answer once no run of the month is going on."""
    deadline = time.monotonic() + 10
    while True:
        answer: dict[str, Any] = dashboard.get(path).json()
        if answer["running"] is None or time.monotonic() > deadline:
            return answer
        time.sleep(0.02)


def record_run(
    ledger: Ledger,
    started_by: str,
    *,
    failing: dict[str, str] | None = None,
    read: tuple[str, ...] = (ENGINEERING, FINANCE),
    finished: bool = True,
    cost: Decimal | None = None,
    month: CollectionMonth = AUGUST,
) -> int:
    started = datetime(2026, 9, 3, 0, 30, tzinfo=UTC)
    run_id = ledger.start_run(month, started_by, started)  # pyright: ignore[reportArgumentType]
    for account in read:
        ledger.record_sync(month, account, (failing or {}).get(account), run_id)
    if finished:
        states = Counter(
            {
                EmailState.COLLECTED: 5,
                EmailState.NEEDS_REVIEW: 1,
                EmailState.SKIPPED: 2,
                EmailState.FAILED: 1,
            }
        )
        ledger.finish_run(run_id, started + timedelta(minutes=3, seconds=20), states, cost)
    return run_id


# Seeing past runs


def test_each_run_shows_its_counts_its_metrics_and_how_it_was_started(
    dashboard: TestClient, ledger: Ledger
) -> None:
    record_run(ledger, "schedule")
    record_run(ledger, "command_line", cost=Decimal("0.42"))

    answer = dashboard.get(RUNS).json()

    latest, earlier = answer["runs"]
    assert (latest["started_by"], earlier["started_by"]) == ("command_line", "schedule")
    assert latest["person"] is None
    assert latest["state"] == "finished"
    assert (latest["collected"], latest["needs_review"], latest["skipped"], latest["failed"]) == (
        5,
        1,
        2,
        1,
    )
    assert latest["emails_found"] == 9
    assert latest["duration_seconds"] == 200
    assert latest["model_cost_usd"] == "0.42"


def test_model_cost_that_was_not_recorded_is_empty_not_zero(
    dashboard: TestClient, ledger: Ledger
) -> None:
    record_run(ledger, "command_line")

    [run] = dashboard.get(RUNS).json()["runs"]

    assert run["model_cost_usd"] is None


def test_a_failed_source_account_shows_why_it_failed(
    dashboard: TestClient, ledger: Ledger, settings: Settings
) -> None:
    connect(settings, ENGINEERING, FINANCE)
    record_run(ledger, "schedule", failing={FINANCE: "the sign-in has expired"})

    [run] = dashboard.get(RUNS).json()["runs"]

    assert run["source_accounts"] == [
        {"source_account": ENGINEERING, "read": True, "reason": None, "can_run_again": False},
        {
            "source_account": FINANCE,
            "read": False,
            "reason": "the sign-in has expired",
            "can_run_again": True,
        },
    ]


def test_a_failure_read_since_by_a_later_run_is_not_offered_again(
    dashboard: TestClient, ledger: Ledger, settings: Settings
) -> None:
    connect(settings, ENGINEERING, FINANCE)
    record_run(ledger, "schedule", failing={FINANCE: "the sign-in has expired"})
    record_run(ledger, "command_line", read=(FINANCE,))

    _, earlier = dashboard.get(RUNS).json()["runs"]

    assert earlier["source_accounts"][1]["reason"] == "the sign-in has expired"
    assert earlier["source_accounts"][1]["can_run_again"] is False


def test_runs_of_another_month_are_not_shown(dashboard: TestClient, ledger: Ledger) -> None:
    record_run(ledger, "schedule", month=JULY)

    assert dashboard.get(RUNS).json()["runs"] == []


def test_a_run_started_elsewhere_that_has_not_finished_is_shown_as_unfinished(
    dashboard: TestClient, ledger: Ledger
) -> None:
    record_run(ledger, "command_line", finished=False)

    [run] = dashboard.get(RUNS).json()["runs"]

    assert run["state"] == "unfinished"
    assert (run["finished_at"], run["duration_seconds"], run["emails_found"]) == (None, None, None)


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_runs_need_a_signed_in_person(
    settings: Settings, runner: FakeRunner, clock: Clock, method: str
) -> None:
    verifier = FakeIdentityVerifier({})
    app = create_app(settings, lambda: Ledger(settings.ledger_path), verifier, runner=runner)
    with TestClient(app, base_url="http://localhost:8000") as client:
        assert client.request(method, RUNS, json={}).status_code == 401
    assert runner.asked == []


# Running a month again


def test_a_month_is_run_again_and_the_answer_comes_at_once(
    dashboard: TestClient, runner: FakeRunner, settings: Settings
) -> None:
    connect(settings, ENGINEERING, FINANCE)
    runner.release.clear()

    started = dashboard.post(RUNS, json={})

    assert started.status_code == 202
    assert started.json() == {"month": "2026-08", "source_account": None}
    assert runner.started.wait(5)
    going = dashboard.get(RUNS).json()
    assert going["running"]["person"] == FINANCE
    assert going["cannot_start"] == f"A run of 2026-08 is already going on, started by {FINANCE}."
    [run] = going["runs"]
    assert (run["state"], run["started_by"], run["person"]) == ("running", "dashboard", FINANCE)

    runner.release.set()
    done = runs_when_done(dashboard)

    [run] = done["runs"]
    assert (run["state"], run["person"], run["only_source_account"]) == (
        "finished",
        FINANCE,
        None,
    )
    assert (run["collected"], run["skipped"]) == (2, 1)
    assert runner.asked == [(AUGUST, None)]


def test_a_second_run_of_the_same_month_is_refused_while_one_is_going_on(
    dashboard: TestClient, runner: FakeRunner, settings: Settings
) -> None:
    connect(settings, ENGINEERING)
    runner.release.clear()
    dashboard.post(RUNS, json={})
    assert runner.started.wait(5)

    again = dashboard.post(RUNS, json={})
    other_month = dashboard.post("/api/months/2026-07/runs", json={})

    assert again.status_code == 409
    assert again.json()["detail"] == (
        f"A run of 2026-08 is already going on, started by {FINANCE}."
    )
    assert other_month.status_code == 202
    runner.release.set()
    runs_when_done(dashboard)
    runs_when_done(dashboard, "/api/months/2026-07/runs")
    assert runner.asked == [(AUGUST, None), (JULY, None)]


def test_a_member_may_run_a_month_again(
    settings: Settings, runner: FakeRunner, clock: Clock
) -> None:
    People(settings.ledger_path, settings.allowlist).add(MEMBER, "member", by=FINANCE, at=NOW)
    connect(settings, ENGINEERING)

    with open_dashboard(settings, runner, clock, "code-member") as member:
        assert member.post(RUNS, json={}).status_code == 202
        [run] = runs_when_done(member)["runs"]

    assert run["person"] == MEMBER


def test_a_month_with_no_connected_source_account_is_not_run(
    dashboard: TestClient, runner: FakeRunner
) -> None:
    refused = dashboard.post(RUNS, json={})

    assert refused.status_code == 409
    assert "No source account is connected" in refused.json()["detail"]
    assert "No source account is connected" in dashboard.get(RUNS).json()["cannot_start"]
    assert runner.asked == []


def test_runs_cannot_be_started_without_a_runner(
    settings: Settings, ledger: Ledger, clock: Clock
) -> None:
    connect(settings, ENGINEERING)
    record_run(ledger, "schedule")

    with open_dashboard(settings, None, clock) as dashboard:
        refused = dashboard.post(RUNS, json={})
        shown = dashboard.get(RUNS).json()

    assert refused.status_code == 503
    assert shown["cannot_start"] == refused.json()["detail"]
    assert len(shown["runs"]) == 1


def test_a_run_that_stops_says_why(
    dashboard: TestClient, runner: FakeRunner, settings: Settings
) -> None:
    connect(settings, ENGINEERING)
    runner.stops_with = "the disk is full"

    dashboard.post(RUNS, json={})
    [run] = runs_when_done(dashboard)["runs"]

    assert (run["state"], run["problem"], run["person"]) == ("stopped", "the disk is full", FINANCE)


def test_a_run_that_did_not_start_is_listed_with_why(
    dashboard: TestClient, runner: FakeRunner, settings: Settings
) -> None:
    connect(settings, ENGINEERING)
    runner.refuses_with = "the owner account is not signed in"

    dashboard.post(RUNS, json={})
    answer = runs_when_done(dashboard)

    assert answer["runs"] == []
    [refused] = answer["not_started"]
    assert (refused["person"], refused["problem"]) == (
        FINANCE,
        "the owner account is not signed in",
    )
    # The next run is not mistaken for the one that did not start.
    runner.refuses_with = None
    dashboard.post(RUNS, json={})
    [run] = runs_when_done(dashboard)["runs"]
    assert (run["state"], run["problem"]) == ("finished", None)


def test_a_run_cut_off_when_the_service_stopped_is_shown_as_stopped_not_running(
    settings: Settings, runner: FakeRunner, ledger: Ledger, clock: Clock
) -> None:
    connect(settings, ENGINEERING)
    # As left behind by a service stopped in the middle of a run it started.
    RunRequests(settings.ledger_path).asked(AUGUST, None, MEMBER, NOW, after_run_id=0)
    record_run(ledger, "dashboard", finished=False)

    with open_dashboard(settings, runner, clock) as dashboard:
        answer = dashboard.get(RUNS).json()

    [run] = answer["runs"]
    assert (run["state"], run["person"]) == ("stopped", MEMBER)
    assert answer["running"] is None
    assert answer["cannot_start"] is None


# Running one failed source account again


def test_a_single_failed_source_account_is_run_again_alone(
    dashboard: TestClient, runner: FakeRunner, ledger: Ledger, settings: Settings
) -> None:
    connect(settings, ENGINEERING, FINANCE)
    record_run(ledger, "schedule", failing={FINANCE: "the sign-in has expired"})

    started = dashboard.post(RUNS, json={"source_account": FINANCE.upper()})

    assert started.status_code == 202
    assert started.json()["source_account"] == FINANCE
    latest, earlier = runs_when_done(dashboard)["runs"]
    assert runner.asked == [(AUGUST, FINANCE)]
    assert latest["only_source_account"] == FINANCE
    assert [s["source_account"] for s in latest["source_accounts"]] == [FINANCE]
    # The earlier run keeps its failure, and it is no longer offered: it has been read.
    assert earlier["source_accounts"][1]["read"] is False
    assert earlier["source_accounts"][1]["can_run_again"] is False


def test_a_source_account_that_did_not_fail_is_not_run_again_alone(
    dashboard: TestClient, runner: FakeRunner, ledger: Ledger, settings: Settings
) -> None:
    connect(settings, ENGINEERING, FINANCE)
    record_run(ledger, "schedule")

    refused = dashboard.post(RUNS, json={"source_account": ENGINEERING})

    assert refused.status_code == 409
    assert "Run the whole month instead" in refused.json()["detail"]
    assert runner.asked == []


def test_a_source_account_that_is_not_connected_is_not_run_again(
    dashboard: TestClient, runner: FakeRunner, ledger: Ledger, settings: Settings
) -> None:
    connect(settings, ENGINEERING)
    record_run(ledger, "schedule", failing={FINANCE: "the sign-in has expired"})

    refused = dashboard.post(RUNS, json={"source_account": FINANCE})

    assert refused.status_code == 409
    assert f"{FINANCE} is not a connected source account" in refused.json()["detail"]
    assert runner.asked == []
