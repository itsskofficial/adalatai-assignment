"""The Settings screen's API, called the way the dashboard calls it."""

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient
from test_api import FINANCE, sign_in
from test_runner import SECRET, Runner, a_runner

from invoice_collector.api.app import create_app
from invoice_collector.api.collection_settings_routes import ScheduleKeeper
from invoice_collector.api.identity import FakeIdentityVerifier
from invoice_collector.api.people import People
from invoice_collector.api.runner_client import RunnerClient
from invoice_collector.api.settings import Settings
from invoice_collector.collection_settings import SettingsStore
from invoice_collector.ledger import Ledger
from invoice_collector.run_starter import RunnerUnreachable

SETTINGS = "/api/settings"
MEMBER = "member@nyayalabs.example"
NOW = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
FIFTH_AT_SIX = {"enabled": True, "day": 5, "time": "06:00", "time_zone": "Asia/Kolkata"}


class Keeper:
    """Stands in for the runner, which keeps the schedule."""

    def __init__(self, unreachable: bool = False) -> None:
        self.told = 0
        self.unreachable = unreachable

    def schedule_changed(self) -> None:
        if self.unreachable:
            raise RunnerUnreachable(
                "The runner cannot be reached at http://runner:8001 (connection refused). "
                "Check that the runner service is running."
            )
        self.told += 1


def open_dashboard(
    ledger_path: Path, keeper: ScheduleKeeper | None, code: str = "code-finance"
) -> TestClient:
    settings = Settings(
        session_secret="a-secret-only-for-tests",
        allowlist=frozenset({FINANCE}),
        ledger_path=ledger_path,
    )
    People(ledger_path, settings.allowlist).add(MEMBER, "member", by=FINANCE, at=NOW)
    app = create_app(
        settings,
        lambda: Ledger(ledger_path),
        FakeIdentityVerifier({"code-finance": FINANCE, "code-member": MEMBER}),
        schedule_keeper=keeper,
        now=lambda: NOW,
    )
    client = TestClient(app, base_url="http://localhost:8000", follow_redirects=False)
    sign_in(client, code)
    return client


@pytest.fixture
def ledger_path(tmp_path: Path) -> Path:
    path = tmp_path / "out" / "ledger.sqlite"
    Ledger(path).close()
    return path


@pytest.fixture
def keeper() -> Keeper:
    return Keeper()


@pytest.fixture
def dashboard(ledger_path: Path, keeper: Keeper) -> Iterator[TestClient]:
    with open_dashboard(ledger_path, keeper) as client:
        yield client


def change(dashboard: TestClient, **changes: Any) -> httpx2.Response:
    current = dashboard.get(SETTINGS).json()
    body = {"schedule": current["schedule"], "drive_folder": current["drive_folder"], **changes}
    return dashboard.put(SETTINGS, json=body)


def test_settings_never_set_have_their_defaults(dashboard: TestClient) -> None:
    shown = dashboard.get(SETTINGS).json()

    assert shown == {
        "schedule": {"enabled": False, "day": 3, "time": "06:00", "time_zone": "Asia/Kolkata"},
        "drive_folder": "Invoice Collection",
        "next_run": None,
        "can_change": True,
        "runner": True,
        "changes": [],
    }


def test_turning_the_schedule_on_shows_the_next_run_and_the_month_it_collects(
    dashboard: TestClient, keeper: Keeper
) -> None:
    saved = change(dashboard, schedule=FIFTH_AT_SIX)

    assert saved.status_code == 200
    answer = saved.json()
    assert answer["next_run"] == {
        "due_at": "2026-09-05T06:00:00+05:30",
        "collection_month": "2026-08",
    }
    assert answer["runner_notice"] == "The runner has the new schedule."
    assert keeper.told == 1
    assert [
        (c["name"], c["value_before"], c["value_after"], c["person"]) for c in answer["changes"]
    ] == [
        ("schedule.day", "3", "5", FINANCE),
        ("schedule.enabled", "off", "on", FINANCE),
    ]
    assert dashboard.get(SETTINGS).json()["next_run"] == answer["next_run"]


def test_a_day_already_past_this_month_is_next_due_next_month(dashboard: TestClient) -> None:
    answer = change(dashboard, schedule={**FIFTH_AT_SIX, "day": 1}).json()

    assert answer["next_run"] == {
        "due_at": "2026-10-01T06:00:00+05:30",
        "collection_month": "2026-09",
    }


def test_the_drive_folder_is_changed_without_telling_the_runner(
    dashboard: TestClient, keeper: Keeper, ledger_path: Path
) -> None:
    answer = change(dashboard, drive_folder="  Finance   invoices ").json()

    assert answer["drive_folder"] == "Finance invoices"
    assert answer["runner_notice"] is None
    assert keeper.told == 0
    assert SettingsStore(ledger_path).read().drive_folder == "Finance invoices"
    [changed] = answer["changes"]
    assert (changed["name"], changed["value_before"], changed["value_after"]) == (
        "drive.folder",
        "Invoice Collection",
        "Finance invoices",
    )


def test_a_change_is_kept_when_the_runner_cannot_be_reached(ledger_path: Path) -> None:
    with open_dashboard(ledger_path, Keeper(unreachable=True)) as dashboard:
        answer = change(dashboard, schedule=FIFTH_AT_SIX).json()

    assert answer["schedule"]["enabled"] is True
    assert answer["runner_notice"] == (
        "The change is saved. The runner cannot be reached at http://runner:8001 (connection "
        "refused). Check that the runner service is running. The runner will pick up the new "
        "schedule when it starts."
    )
    assert SettingsStore(ledger_path).read().schedule.day == 5


def test_without_a_runner_the_screen_says_nothing_runs_on_the_schedule(ledger_path: Path) -> None:
    with open_dashboard(ledger_path, None) as dashboard:
        assert dashboard.get(SETTINGS).json()["runner"] is False
        answer = change(dashboard, schedule=FIFTH_AT_SIX).json()

    assert "No runner service is set up" in answer["runner_notice"]


def test_members_see_the_settings_and_cannot_change_them(ledger_path: Path) -> None:
    with open_dashboard(ledger_path, Keeper(), code="code-member") as dashboard:
        shown = dashboard.get(SETTINGS).json()
        refused = change(dashboard, schedule=FIFTH_AT_SIX)

    assert shown["can_change"] is False
    assert refused.status_code == 403
    assert refused.json()["detail"] == "Only an administrator can change the settings"
    assert SettingsStore(ledger_path).read().schedule.enabled is False


@pytest.mark.parametrize(
    ("changes", "why"),
    [
        ({"schedule": {**FIFTH_AT_SIX, "time_zone": "Mars/Olympus_Mons"}}, "not a time zone"),
        ({"drive_folder": "Finance/2026"}, "cannot hold a /"),
        ({"drive_folder": "   "}, "needs a name"),
    ],
)
def test_a_setting_that_cannot_be_kept_is_refused_with_why(
    dashboard: TestClient, changes: dict[str, Any], why: str
) -> None:
    refused = change(dashboard, **changes)

    assert refused.status_code == 422
    assert why in refused.json()["detail"]


@pytest.mark.parametrize(
    "schedule",
    [{**FIFTH_AT_SIX, "day": 29}, {**FIFTH_AT_SIX, "day": 0}, {**FIFTH_AT_SIX, "time": "25:00"}],
)
def test_a_day_or_time_that_cannot_be_is_refused(
    dashboard: TestClient, schedule: dict[str, Any]
) -> None:
    assert change(dashboard, schedule=schedule).status_code == 422
    assert dashboard.get(SETTINGS).json()["schedule"]["enabled"] is False


def test_settings_need_a_signed_in_person(ledger_path: Path) -> None:
    settings = Settings(
        session_secret="a-secret-only-for-tests",
        allowlist=frozenset({FINANCE}),
        ledger_path=ledger_path,
    )
    app = create_app(settings, lambda: Ledger(ledger_path), FakeIdentityVerifier({}))
    with TestClient(app) as nobody:
        assert nobody.get(SETTINGS).status_code == 401
        assert nobody.put(SETTINGS, json={}).status_code == 401


def test_the_runner_works_out_its_next_run_again_when_the_schedule_changes(
    tmp_path: Path,
) -> None:
    runner: Runner = a_runner(tmp_path, NOW)
    runner.service.start()
    client = RunnerClient("http://runner:8001", SECRET, http=runner.api)

    with open_dashboard(runner.ledger_path, client) as dashboard:
        change(dashboard, schedule=FIFTH_AT_SIX)

    health = runner.api.get("/health").json()
    assert health["next_scheduled_run"] == {
        "due_at": datetime(2026, 9, 5, 0, 30, tzinfo=UTC).isoformat(),
        "collection_month": "2026-08",
    }
    [timer] = runner.timers.waiting
    assert timer.seconds == (datetime(2026, 9, 5, 0, 30, tzinfo=UTC) - NOW).total_seconds()
