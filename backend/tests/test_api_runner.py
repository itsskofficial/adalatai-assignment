"""The Runs screen's API when the runner service performs the runs.

The dashboard is given the client it uses in production, talking to the runner's own HTTP
API in memory, with the fake run function and hand-moved clock of test_runner.py.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from test_api import FINANCE, sign_in
from test_runner import AUGUST, SECRET, THIRD, Runner, a_runner

from invoice_collector.api.app import create_app
from invoice_collector.api.identity import FakeIdentityVerifier
from invoice_collector.api.runner_client import RunnerClient
from invoice_collector.api.serve import main as serve_main
from invoice_collector.api.settings import Settings
from invoice_collector.ledger import Ledger
from invoice_collector.source_account_registry import SourceAccountRegistry

RUNS = "/api/months/2026-08/runs"
OPS = "ops@nyayalabs.example"
NOW = datetime(2026, 9, 1, tzinfo=UTC)


@dataclass
class Deployed:
    """The app and the runner over one ledger, as deployed on one machine."""

    runner: Runner
    dashboard: TestClient

    def runs(self) -> dict[str, Any]:
        answer = self.dashboard.get(RUNS)
        assert answer.status_code == 200
        return answer.json()


def dashboard_for(ledger_path: Path, client: RunnerClient) -> TestClient:
    settings = Settings(
        session_secret="a-secret-only-for-tests",
        allowlist=frozenset({FINANCE}),
        ledger_path=ledger_path,
    )
    app = create_app(
        settings,
        lambda: Ledger(ledger_path),
        FakeIdentityVerifier({"code-finance": FINANCE}),
        runs=client,
        now=lambda: NOW,
    )
    dashboard = TestClient(app, base_url="http://localhost:8000", follow_redirects=False)
    sign_in(dashboard)
    return dashboard


@pytest.fixture
def deployed(tmp_path: Path) -> Iterator[Deployed]:
    runner = a_runner(tmp_path, NOW)
    SourceAccountRegistry(runner.ledger_path).signed_in(
        OPS, "connected", FINANCE, NOW, signed_in_at=None, fingerprint=None
    )
    client = RunnerClient("http://runner:8001", SECRET, http=runner.api)
    with dashboard_for(runner.ledger_path, client) as dashboard:
        yield Deployed(runner, dashboard)
    runner.run.release.set()
    runner.service.starter.wait(10)


def record_run(ledger_path: Path, started_by: str) -> None:
    ledger = Ledger(ledger_path)
    try:
        ledger.start_run(AUGUST, started_by, NOW)  # pyright: ignore[reportArgumentType]
    finally:
        ledger.close()


def test_a_run_started_on_the_runs_screen_is_performed_by_the_runner(deployed: Deployed) -> None:
    deployed.runner.run.release.clear()

    answer = deployed.dashboard.post(RUNS, json={})
    assert deployed.runner.run.started.wait(10)
    going = deployed.runs()

    assert answer.status_code == 202
    [run] = going["runs"]
    assert (run["state"], run["started_by"], run["person"]) == ("running", "dashboard", FINANCE)
    assert going["running"]["person"] == FINANCE
    assert going["cannot_start"] == f"A run of 2026-08 is already going on, started by {FINANCE}."
    assert deployed.dashboard.post(RUNS, json={}).status_code == 409

    deployed.runner.run.release.set()
    deployed.runner.service.starter.wait(10)
    done = deployed.runs()

    assert [run["state"] for run in done["runs"]] == ["finished"]
    assert done["running"] is None and done["cannot_start"] is None
    assert done["runner_problem"] is None


def test_a_scheduled_run_going_on_in_the_runner_is_shown_as_running(deployed: Deployed) -> None:
    deployed.runner.keep(THIRD, NOW)
    deployed.runner.service.start()
    deployed.runner.run.release.clear()
    deployed.runner.clock.now = datetime(2026, 9, 3, 0, 30, tzinfo=UTC)
    deployed.runner.timers.fire()
    assert deployed.runner.run.started.wait(10)

    going = deployed.runs()

    [run] = going["runs"]
    assert (run["state"], run["started_by"], run["person"]) == ("running", "schedule", None)
    assert going["running"]["started_by"] == "schedule"
    assert going["running"]["person"] is None
    assert going["cannot_start"] == "A run of 2026-08 is already going on, started by the schedule."


def test_an_unfinished_run_the_runner_no_longer_performs_is_shown_as_stopped(
    deployed: Deployed,
) -> None:
    for started_by in ("command_line", "schedule", "dashboard"):
        record_run(deployed.runner.ledger_path, started_by)

    states = {run["started_by"]: run["state"] for run in deployed.runs()["runs"]}

    # The runner performs runs from the dashboard and the schedule, so one it no longer
    # performs has stopped. A run from the command line may go on elsewhere.
    assert states == {"dashboard": "stopped", "schedule": "stopped", "command_line": "unfinished"}


def refusing_connections(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


def test_a_runner_that_cannot_be_reached_is_said_plainly_and_nothing_else_breaks(
    tmp_path: Path,
) -> None:
    ledger_path = tmp_path / "out" / "ledger.sqlite"
    record_run(ledger_path, "dashboard")
    SourceAccountRegistry(ledger_path).signed_in(
        OPS, "connected", FINANCE, NOW, signed_in_at=None, fingerprint=None
    )
    down = RunnerClient(
        "http://runner:8001",
        SECRET,
        http=httpx.Client(transport=httpx.MockTransport(refusing_connections)),
    )

    with dashboard_for(ledger_path, down) as dashboard:
        shown = dashboard.get(RUNS).json()
        started = dashboard.post(RUNS, json={})
        summary = dashboard.get("/api/months/2026-08/summary")

    problem = (
        "The runner cannot be reached at http://runner:8001 (connection refused). "
        "Check that the runner service is running."
    )
    assert shown["runner_problem"] == problem
    assert shown["cannot_start"] == problem
    # Whether the run goes on cannot be told, so it is not called stopped.
    assert [run["state"] for run in shown["runs"]] == ["unfinished"]
    assert (started.status_code, started.json()["detail"]) == (503, problem)
    assert summary.status_code == 200


def test_a_runner_given_another_secret_says_so(deployed: Deployed) -> None:
    wrong = RunnerClient("http://runner:8001", "not-the-secret", http=deployed.runner.api)

    with dashboard_for(deployed.runner.ledger_path, wrong) as dashboard:
        shown = dashboard.get(RUNS).json()

    assert "the shared secret" in shown["runner_problem"]
    assert "INVOICE_COLLECTOR_RUNNER_SECRET" in shown["runner_problem"]


# The dashboard command


def start_dashboard(tmp_path: Path, environment: dict[str, str], *options: str) -> int:
    web_client_file = tmp_path / "web-client.json"
    web_client_file.write_text(
        '{"web": {"client_id": "made-up-id", "client_secret": "made-up-value"}}',
        encoding="utf-8",
    )
    return serve_main(
        ["--ledger", str(tmp_path / "out" / "ledger.sqlite"), *options],
        environment={
            "INVOICE_COLLECTOR_SESSION_SECRET": "a-secret-only-for-tests",
            "INVOICE_COLLECTOR_ALLOWLIST": FINANCE,
            "INVOICE_COLLECTOR_WEB_CLIENT_FILE": str(web_client_file),
            **environment,
        },
        serve=lambda app: None,
    )


def test_the_dashboard_asks_the_runner_when_its_address_is_set(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = start_dashboard(
        tmp_path,
        {
            "INVOICE_COLLECTOR_RUNNER_URL": "http://runner:8001",
            "INVOICE_COLLECTOR_RUNNER_SECRET": SECRET,
        },
    )

    assert code == 0
    assert "Runs are asked of the runner service at http://runner:8001" in capsys.readouterr().err


def test_the_dashboard_will_not_ask_a_runner_without_the_secret(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = start_dashboard(tmp_path, {"INVOICE_COLLECTOR_RUNNER_URL": "http://runner:8001"})

    assert code == 2
    assert "INVOICE_COLLECTOR_RUNNER_SECRET is not" in capsys.readouterr().err


def test_run_options_belong_to_the_runner_when_it_performs_the_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = start_dashboard(
        tmp_path,
        {
            "INVOICE_COLLECTOR_RUNNER_URL": "http://runner:8001",
            "INVOICE_COLLECTOR_RUNNER_SECRET": SECRET,
        },
        "--run-options=--no-exchange-rates",
    )

    assert code == 2
    assert "--run-options are given to the runner service" in capsys.readouterr().err
