"""The runner: performs runs asked for by the app, and runs the schedule.

The schedule is held as a timer. The runner works out the next moment a scheduled run is
due from the settings in the ledger, sleeps until then, and runs; it does not wake to check.
When the app says the settings changed, it works the moment out again. When it starts, it
looks once for a scheduled run that was due while it was not running, and performs it.

Every run is started through one RunStarter, so a scheduled run and one asked for from the
dashboard are never two runs of one month at once.
"""

import logging
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Protocol

from invoice_collector.collection_settings import SCHEDULE_SETTINGS, SettingsStore
from invoice_collector.ledger import Ledger
from invoice_collector.run_starter import RunRefused, RunStarter
from invoice_collector.schedule import Due, last_due, next_due, seconds_until

_log = logging.getLogger(__name__)
_A_MOMENT = timedelta(microseconds=1)


class Timer(Protocol):
    def cancel(self) -> None: ...


# Calls the action once, after that many seconds, unless cancelled first.
TimerFactory = Callable[[float, Callable[[], None]], Timer]


def threading_timer(seconds: float, action: Callable[[], None]) -> Timer:
    timer = threading.Timer(seconds, action)
    timer.daemon = True
    timer.start()
    return timer


class RunnerService:
    def __init__(
        self,
        starter: RunStarter,
        settings: SettingsStore,
        ledger_factory: Callable[[], Ledger],
        clock: Callable[[], datetime],
        timer: TimerFactory = threading_timer,
    ) -> None:
        self.starter = starter
        self._settings = settings
        self._ledger_factory = ledger_factory
        self._clock = clock
        self._timer_factory = timer
        self._lock = threading.Lock()
        self._timer: Timer | None = None
        self._next: Due | None = None
        # Counts each time the timer is set, so a timer set before the latest does nothing.
        self._armed = 0

    @property
    def next_due(self) -> Due | None:
        """The next scheduled run, or None when the schedule is off."""
        with self._lock:
            return self._next

    def start(self) -> None:
        """Performs a scheduled run missed while the runner was not running, then sets the
        timer for the next."""
        self._catch_up()
        self.schedule_changed()

    def stop(self) -> None:
        with self._lock:
            self._armed += 1
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    def schedule_changed(self) -> Due | None:
        """Works out the next scheduled run from the settings again, and sets the timer."""
        schedule = self._settings.read().schedule
        with self._lock:
            self._armed += 1
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            now = self._clock()
            self._next = next_due(schedule, now)
            if self._next is not None:
                armed, due = self._armed, self._next
                self._timer = self._timer_factory(
                    seconds_until(due, now), lambda: self._due(armed, due)
                )
                _log.info(
                    "The next scheduled run, of %s, is due at %s",
                    due.collection_month,
                    due.at.isoformat(),
                )
            return self._next

    def _due(self, armed: int, due: Due) -> None:
        with self._lock:
            if armed != self._armed:
                return  # Set before the schedule changed.
            early = self._clock() < due.at
        # The schedule may have changed while the app could not tell the runner, so the
        # moment is checked against the settings as they are now.
        if next_due(self._settings.read().schedule, due.at - _A_MOMENT) != due:
            self.schedule_changed()
            return
        if early:
            # A timer counts time as the machine does, which may differ a little from the
            # clock; it is set again for what is left.
            self._set_again(armed, due)
            return
        self._perform(due)
        self.schedule_changed()

    def _set_again(self, armed: int, due: Due) -> None:
        with self._lock:
            if armed != self._armed:
                return
            self._timer = self._timer_factory(
                seconds_until(due, self._clock()), lambda: self._due(armed, due)
            )

    def _perform(self, due: Due) -> None:
        try:
            self.starter.start_scheduled(due.collection_month)
        except RunRefused as refused:
            # A run of the month is going on already, and collects what this one would.
            _log.warning("The scheduled run of %s did not start: %s", due.collection_month, refused)
        else:
            _log.info("Started the scheduled run of %s", due.collection_month)

    def missed(self) -> Due | None:
        """The scheduled run due most recently, if it was due after the schedule was set and
        no run of its month has finished since it was due."""
        schedule = self._settings.read().schedule
        now = self._clock()
        due = last_due(schedule, now)
        set_at = self._settings.last_changed(SCHEDULE_SETTINGS)
        if due is None or set_at is None or set_at > due.at:
            return None
        ledger = self._ledger_factory()
        try:
            runs = ledger.runs(due.collection_month)
        finally:
            ledger.close()
        if any(run.finished_at is not None and run.started_at >= due.at for run in runs):
            return None
        return due

    def _catch_up(self) -> None:
        due = self.missed()
        if due is not None:
            _log.info(
                "The scheduled run of %s, due at %s, was missed; running it now",
                due.collection_month,
                due.at.isoformat(),
            )
            self._perform(due)
