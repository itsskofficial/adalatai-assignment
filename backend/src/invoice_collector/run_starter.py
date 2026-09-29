"""Starting runs in the background, one at a time for each collection month.

The dashboard and the runner service both start runs this way: the answer comes at once,
and the run goes on on a thread of its own. Who asked is kept in the run requests table
(run_requests.py). A scheduled run has nobody to name, so it is kept only while it goes on,
with the latest run id when it started, which is how its run is found in the ledger.

The run itself is performed by a Runner, which does what the command line does (ADR 0010).
"""

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from invoice_collector.domain import CollectionMonth, Run, StartedBy
from invoice_collector.ledger import Ledger
from invoice_collector.run_requests import RunRequests, runs_of

LedgerFactory = Callable[[], Ledger]

_log = logging.getLogger(__name__)


class Runner(Protocol):
    """Performs one run, as the command line does."""

    def __call__(
        self,
        month: CollectionMonth,
        source_account: str | None,
        *,
        started_by: StartedBy = "dashboard",
    ) -> None:
        """Collects the month, reading only the source account given, or every connected
        one when none is, and records the run as started_by. Returns once the run is over.

        Raises when the run could not start or stopped, with the reason as its message.
        """
        ...


class RunRefused(Exception):
    """A run may not start just now. The message says why in plain words."""


class RunnerUnreachable(Exception):
    """The runner service could not be asked. The message says why in plain words."""


@dataclass(frozen=True)
class ActiveRun:
    """A run going on."""

    collection_month: CollectionMonth
    started_by: StartedBy
    # Who asked, for a run started from the dashboard.
    person: str | None
    # The one source account it reads, or None for every connected one.
    source_account: str | None
    requested_at: datetime
    # The latest run in the ledger when it was asked for; 0 when there was none. Its own
    # run is the first after it that was started the same way.
    after_run_id: int
    # The dashboard's request, for a run started from the dashboard.
    request_id: int | None = None

    def run_in(self, runs: list[Run]) -> Run | None:
        """The run this is performing, among the runs of its month, once it is recorded."""
        return min(
            (
                run
                for run in runs
                if run.collection_month == self.collection_month
                and run.started_by == self.started_by
                and run.id > self.after_run_id
            ),
            key=lambda run: run.id,
            default=None,
        )


class Runs(Protocol):
    """Starts runs and says which are going on: in this service, or in the runner service."""

    # How the runs it performs are started. An unfinished run started one of these ways that
    # is not going on has stopped; one started another way may go on elsewhere.
    performs: frozenset[StartedBy]

    def active(self, month: CollectionMonth) -> ActiveRun | None:
        """The run of the month going on, if any. Raises RunnerUnreachable."""
        ...

    def start(self, month: CollectionMonth, source_account: str | None, person: str) -> None:
        """Starts a run asked for by a person, and returns at once.

        Raises RunRefused while a run of the month goes on, and RunnerUnreachable.
        """
        ...


class RunStarter:
    """Starts runs on threads of their own, one at a time for each collection month."""

    performs: frozenset[StartedBy] = frozenset({"dashboard"})

    def __init__(
        self,
        runner: Runner,
        requests: RunRequests,
        ledger_factory: LedgerFactory,
        now: Callable[[], datetime],
        *,
        performs: frozenset[StartedBy] = frozenset({"dashboard"}),
    ) -> None:
        self._runner = runner
        self._requests = requests
        self._ledger_factory = ledger_factory
        self._now = now
        self.performs = performs
        self._lock = threading.Lock()
        self._active: dict[CollectionMonth, ActiveRun] = {}
        self._threads: list[threading.Thread] = []

    def active(self, month: CollectionMonth) -> ActiveRun | None:
        with self._lock:
            return self._active.get(month)

    def going_on(self) -> list[ActiveRun]:
        """Every run going on, oldest first."""
        with self._lock:
            return sorted(self._active.values(), key=lambda run: run.requested_at)

    def start(self, month: CollectionMonth, source_account: str | None, person: str) -> None:
        """Starts the run and returns at once. Raises RunRefused while one is going on."""
        self._start(month, source_account, person, "dashboard")

    def start_scheduled(self, month: CollectionMonth) -> None:
        """Starts the scheduled run of the month. Raises RunRefused while one is going on."""
        self._start(month, None, None, "schedule")

    def _start(
        self,
        month: CollectionMonth,
        source_account: str | None,
        person: str | None,
        started_by: StartedBy,
    ) -> None:
        with self._lock:
            going = self._active.get(month)
            if going is not None:
                who = f"started by {going.person}" if going.person else "started by the schedule"
                raise RunRefused(
                    f"A run of {month} is already going on, {who}. "
                    "Wait for it to finish, then start another."
                )
            at = self._now()
            after = self._latest_run_id()
            request_id = (
                self._requests.asked(month, source_account, person, at, after)
                if person is not None
                else None
            )
            self._active[month] = ActiveRun(
                month, started_by, person, source_account, at, after, request_id
            )
            # A daemon, so stopping the service stops the run: it is then shown as stopped.
            thread = threading.Thread(
                target=self._perform,
                args=(month, source_account, started_by, request_id),
                name=f"run-{month}",
                daemon=True,
            )
            self._threads.append(thread)
        try:
            thread.start()
        except RuntimeError as refused:
            # No thread could be made, so nothing will end the run: it is ended here, or the
            # month would be refused as going on until the service restarts.
            with self._lock:
                self._active.pop(month, None)
                self._threads.remove(thread)
            self._requests.ended(
                request_id,
                self._now(),
                started_a_run=False,
                problem=f"the run could not be started: {refused}",
            )
            raise

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

    def _perform(
        self,
        month: CollectionMonth,
        source_account: str | None,
        started_by: StartedBy,
        request_id: int | None,
    ) -> None:
        problem: str | None = None
        try:
            self._runner(month, source_account, started_by=started_by)
        except Exception as error:
            problem = str(error) or type(error).__name__
            _log.warning("The run of %s started by %s stopped: %s", month, started_by, problem)
        finally:
            try:
                if request_id is not None:
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
