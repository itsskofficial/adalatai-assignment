"""Runs asked for on the Runs screen: who asked, for which month and source account.

A run records how it was started in the ledger, but not who started it: the command line
and the schedule have nobody to name. So each request from the dashboard is kept here, in
a table of its own in the ledger's SQLite file, as the source account registry keeps its
own. The Ledger knows nothing of it.

A request is tied to its run by the id of the latest run recorded when the run was about
to start: its run is the first run started from the dashboard for that month after it.
Runs of one month are started from the dashboard one at a time, so no other can come
between. A request whose run never started says so, and is tied to no run.
"""

import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from invoice_collector.database import connect
from invoice_collector.domain import CollectionMonth, Run

SCHEMA = """
CREATE TABLE IF NOT EXISTS dashboard_run_requests (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    collection_month TEXT NOT NULL,
    source_account   TEXT,
    person           TEXT NOT NULL,
    requested_at     TEXT NOT NULL,
    after_run_id     INTEGER NOT NULL,
    ended_at         TEXT,
    problem          TEXT,
    started_a_run    INTEGER
);
"""


@dataclass(frozen=True)
class RunRequest:
    id: int
    collection_month: CollectionMonth
    # Only this source account is read. None reads every connected source account.
    source_account: str | None
    person: str
    requested_at: datetime
    # The latest run in the ledger when this one was asked for; 0 when there was none.
    after_run_id: int
    # None while the run goes on, and for good when the service stopped during it.
    ended_at: datetime | None
    # Why the run stopped or did not start, when it did either.
    problem: str | None
    # False when it ended without recording a run. None until it ends.
    started_a_run: bool | None


def _time(text: str | None) -> datetime | None:
    return datetime.fromisoformat(text) if text is not None else None


class RunRequests:
    def __init__(self, ledger_path: Path) -> None:
        self._path = ledger_path

    def _connect(self) -> sqlite3.Connection:
        db = connect(self._path)
        db.executescript(SCHEMA)
        return db

    def asked(
        self,
        month: CollectionMonth,
        source_account: str | None,
        person: str,
        at: datetime,
        after_run_id: int,
    ) -> int:
        """Records that a person asked for a run, and gives the request's id."""
        with closing(self._connect()) as db, db:
            cursor = db.execute(
                "INSERT INTO dashboard_run_requests (collection_month, source_account, person, "
                "requested_at, after_run_id) VALUES (?, ?, ?, ?, ?)",
                (str(month), source_account, person, at.isoformat(), after_run_id),
            )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def ended(
        self, request_id: int, at: datetime, *, started_a_run: bool, problem: str | None
    ) -> None:
        with closing(self._connect()) as db, db:
            db.execute(
                "UPDATE dashboard_run_requests SET ended_at = ?, problem = ?, started_a_run = ? "
                "WHERE id = ?",
                (at.isoformat(), problem, started_a_run, request_id),
            )

    def of(self, month: CollectionMonth) -> list[RunRequest]:
        """Every request for the month, oldest first."""
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT id, collection_month, source_account, person, requested_at, "
                "after_run_id, ended_at, problem, started_a_run FROM dashboard_run_requests "
                "WHERE collection_month = ? ORDER BY id",
                (str(month),),
            ).fetchall()
        return [
            RunRequest(
                id=request_id,
                collection_month=CollectionMonth.parse(collection_month),
                source_account=source_account,
                person=person,
                requested_at=datetime.fromisoformat(requested_at),
                after_run_id=after_run_id,
                ended_at=_time(ended_at),
                problem=problem,
                started_a_run=None if started_a_run is None else bool(started_a_run),
            )
            for (
                request_id,
                collection_month,
                source_account,
                person,
                requested_at,
                after_run_id,
                ended_at,
                problem,
                started_a_run,
            ) in rows
        ]


def runs_of(requests: Sequence[RunRequest], runs: Sequence[Run]) -> dict[int, Run]:
    """The run each request started, by request id, for the requests that started one.

    A request's run started after it was asked for and before the next request for the
    same month was, since the next could only be accepted once this one had ended.
    """
    started: dict[int, Run] = {}
    ordered = sorted(requests, key=lambda r: r.id)
    for index, request in enumerate(ordered):
        if request.started_a_run is False:
            continue
        later = [r.after_run_id for r in ordered[index + 1 :]]
        before = later[0] if later else None
        run = min(
            (
                run
                for run in runs
                if run.started_by == "dashboard"
                and run.collection_month == request.collection_month
                and run.id > request.after_run_id
                and (before is None or run.id <= before)
            ),
            key=lambda run: run.id,
            default=None,
        )
        if run is not None:
            started[request.id] = run
    return started
