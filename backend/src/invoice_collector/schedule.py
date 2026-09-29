"""When a scheduled run is due, and which collection month it collects.

The schedule is a day of the month, a time of day and a time zone, kept in the ledger's
settings (see collection_settings.py). On that day of month M, at that time in that zone,
the runner collects the collection month M-1: the month that has just ended, whose invoices
have arrived by then. The moment is worked out in the zone, so a run due at 00:30 on the
3rd in India is due at 19:00 on the 2nd in UTC and still collects the month before the 3rd's.

Pure functions of the settings and a moment; nothing here reads a clock.
"""

import calendar
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from invoice_collector.domain import CollectionMonth

DEFAULT_TIME_ZONE = "Asia/Kolkata"
DEFAULT_DAY = 3
DEFAULT_TIME = time(6, 0)
# Every month has a 28th, so a day up to it is due in every month, February included.
LAST_DAY = 28


class ScheduleRefused(Exception):
    """The schedule cannot be kept as given. The message says why in plain words."""


@dataclass(frozen=True)
class Schedule:
    enabled: bool = False
    day: int = DEFAULT_DAY
    at: time = DEFAULT_TIME
    time_zone: str = DEFAULT_TIME_ZONE

    def check(self) -> None:
        """Raises ScheduleRefused when the schedule cannot be kept."""
        if not 1 <= self.day <= LAST_DAY:
            raise ScheduleRefused(
                f"The day of the month must be from 1 to {LAST_DAY}, so it falls in every "
                "month, February included."
            )
        zone(self.time_zone)

    @property
    def zone(self) -> ZoneInfo:
        return zone(self.time_zone)


@dataclass(frozen=True)
class Due:
    """A scheduled run: when it is due, and the collection month it collects."""

    at: datetime
    collection_month: CollectionMonth


def zone(name: str) -> ZoneInfo:
    """The time zone of that name, such as Asia/Kolkata. Raises ScheduleRefused."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ScheduleRefused(
            f"{name} is not a time zone. Give one by its name, such as Asia/Kolkata."
        ) from None


def month_before(moment: datetime, time_zone: ZoneInfo) -> CollectionMonth:
    """The collection month a run due at this moment collects: the month before, in the zone."""
    day = moment.astimezone(time_zone).date()
    if day.month == 1:
        return CollectionMonth(day.year - 1, 12)
    return CollectionMonth(day.year, day.month - 1)


def _due_in(schedule: Schedule, year: int, month: int) -> datetime:
    """The moment the run of that calendar month is due, in UTC.

    A day past the end of a short month falls on its last day. A time that does not exist
    in the zone, skipped when the clocks go forward, is the moment the clocks reached; a
    time that happens twice, when they go back, is the first of the two.
    """
    day = min(schedule.day, calendar.monthrange(year, month)[1])
    local = datetime.combine(datetime(year, month, day).date(), schedule.at, schedule.zone)
    return local.astimezone(UTC)


def _months_from(year: int, month: int, step: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + step
    return index // 12, index % 12 + 1


def _due(schedule: Schedule, at: datetime) -> Due:
    return Due(at, month_before(at, schedule.zone))


def next_due(schedule: Schedule, after: datetime) -> Due | None:
    """The first scheduled run due after the moment, or None when the schedule is off."""
    if not schedule.enabled:
        return None
    local = after.astimezone(schedule.zone)
    for step in range(3):
        year, month = _months_from(local.year, local.month, step - 1)
        at = _due_in(schedule, year, month)
        if at > after:
            return _due(schedule, at)
    raise AssertionError("a monthly schedule is due within two months")  # pragma: no cover


def last_due(schedule: Schedule, at_or_before: datetime) -> Due | None:
    """The latest scheduled run due at or before the moment, or None when the schedule is off."""
    if not schedule.enabled:
        return None
    local = at_or_before.astimezone(schedule.zone)
    for step in range(3):
        year, month = _months_from(local.year, local.month, 1 - step)
        at = _due_in(schedule, year, month)
        if at <= at_or_before:
            return _due(schedule, at)
    raise AssertionError("a monthly schedule was due within two months")  # pragma: no cover


def seconds_until(due: Due, now: datetime) -> float:
    return max((due.at - now) / timedelta(seconds=1), 0.0)
