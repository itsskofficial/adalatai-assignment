"""The app's way to the runner service: starting a run there, and asking what goes on.

Chosen when the runner's address is set. The app never collects: it asks the runner over
the private network between them, sending the shared secret, and the runner answers at
once. Nothing polls; the Runs screen asks what goes on when it is read.

When the runner cannot be reached, each call says so in plain words and nothing else in
the dashboard depends on it.
"""

from datetime import datetime
from typing import Any, Protocol

import httpx

from invoice_collector.domain import CollectionMonth, StartedBy
from invoice_collector.run_starter import ActiveRun, RunnerUnreachable, RunRefused

# The runner answers at once, whatever it does after, so a longer wait means it is not there.
TIMEOUT_SECONDS = 5.0


class Answer(Protocol):
    @property
    def status_code(self) -> int: ...

    @property
    def is_error(self) -> bool: ...

    def json(self) -> Any: ...


class Http(Protocol):
    """Sends a request and gives the answer, as httpx.Client does. Raises httpx.HTTPError
    when nothing answers."""

    def request(
        self, method: str, url: str, *, json: Any = None, headers: dict[str, str] | None = None
    ) -> Answer: ...


class RunnerClient:
    """Starts runs in the runner service, which performs dashboard and scheduled runs."""

    performs: frozenset[StartedBy] = frozenset({"dashboard", "schedule"})

    def __init__(self, url: str, secret: str, http: Http | None = None) -> None:
        self._url = url.rstrip("/")
        self._http: Http = http or httpx.Client(timeout=TIMEOUT_SECONDS)
        self._headers = {"Authorization": f"Bearer {secret}"}

    def _unreachable(self, why: str) -> RunnerUnreachable:
        return RunnerUnreachable(
            f"The runner cannot be reached at {self._url} ({why}). Check that the runner "
            "service is running."
        )

    def _ask(self, method: str, path: str, body: dict[str, Any] | None = None) -> Answer:
        try:
            response = self._http.request(
                method, f"{self._url}{path}", json=body, headers=self._headers
            )
        except httpx.HTTPError as error:
            raise self._unreachable(str(error) or type(error).__name__) from None
        if response.status_code == 401:
            raise RunnerUnreachable(
                "The runner refused the dashboard: the shared secret the two are given "
                "(INVOICE_COLLECTOR_RUNNER_SECRET) differs."
            )
        if response.status_code == 409:
            raise RunRefused(str(response.json().get("detail", "The runner refused the run.")))
        if response.is_error:
            raise self._unreachable(f"it answered {response.status_code}")
        return response

    def active(self, month: CollectionMonth) -> ActiveRun | None:
        going: list[dict[str, Any]] = self._ask("GET", "/runs").json()["runs"]
        for run in going:
            if run["collection_month"] == str(month):
                return ActiveRun(
                    collection_month=month,
                    started_by=run["started_by"],
                    person=run["person"],
                    source_account=run["source_account"],
                    requested_at=datetime.fromisoformat(run["requested_at"]),
                    after_run_id=run["after_run_id"],
                    request_id=run["request_id"],
                )
        return None

    def start(self, month: CollectionMonth, source_account: str | None, person: str) -> None:
        self._ask(
            "POST",
            "/runs",
            {"month": str(month), "source_account": source_account, "person": person},
        )

    def schedule_changed(self) -> None:
        """Tells the runner the schedule changed, so it works out the next run again."""
        self._ask("POST", "/schedule")
