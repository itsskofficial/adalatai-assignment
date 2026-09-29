"""When a scheduled run is due, and which collection month it collects."""

from datetime import UTC, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from invoice_collector.collection_settings import (
    SCHEDULE_DAY,
    SCHEDULE_SETTINGS,
    CollectionSettings,
    SettingsStore,
)
from invoice_collector.domain import CollectionMonth
from invoice_collector.schedule import (
    Schedule,
    ScheduleRefused,
    last_due,
    month_before,
    next_due,
)

KOLKATA = ZoneInfo("Asia/Kolkata")
NEW_YORK = ZoneInfo("America/New_York")
THIRD_AT_SIX = Schedule(enabled=True, day=3, at=time(6, 0), time_zone="Asia/Kolkata")


def test_a_run_due_on_the_third_collects_the_month_that_has_just_ended() -> None:
    due = next_due(THIRD_AT_SIX, datetime(2026, 9, 1, tzinfo=UTC))

    assert due is not None
    assert due.at == datetime(2026, 9, 3, 6, 0, tzinfo=KOLKATA)
    assert due.collection_month == CollectionMonth(2026, 8)


def test_a_run_due_in_january_collects_december_of_the_year_before() -> None:
    due = next_due(THIRD_AT_SIX, datetime(2026, 12, 20, tzinfo=UTC))

    assert due is not None
    assert due.at == datetime(2027, 1, 3, 6, 0, tzinfo=KOLKATA)
    assert due.collection_month == CollectionMonth(2026, 12)


def test_the_moment_just_past_is_not_due_again() -> None:
    due = next_due(THIRD_AT_SIX, datetime(2026, 9, 3, 6, 0, tzinfo=KOLKATA))

    assert due is not None
    assert due.at == datetime(2026, 10, 3, 6, 0, tzinfo=KOLKATA)
    assert due.collection_month == CollectionMonth(2026, 9)


def test_the_moment_is_worked_out_in_the_time_zone() -> None:
    # 00:30 on the 1st in India is still the last day of the month before in UTC, and the
    # run collects the month before the 1st in India.
    first = Schedule(enabled=True, day=1, at=time(0, 30), time_zone="Asia/Kolkata")

    due = next_due(first, datetime(2026, 9, 15, tzinfo=UTC))

    assert due is not None
    assert due.at == datetime(2026, 9, 30, 19, 0, tzinfo=UTC)
    assert due.collection_month == CollectionMonth(2026, 9)
    assert month_before(due.at, KOLKATA) == CollectionMonth(2026, 9)
    assert month_before(due.at, ZoneInfo("UTC")) == CollectionMonth(2026, 8)


def test_the_twenty_eighth_is_due_in_february() -> None:
    last = Schedule(enabled=True, day=28, at=time(23, 30), time_zone="Asia/Kolkata")

    due = next_due(last, datetime(2027, 2, 1, tzinfo=UTC))

    assert due is not None
    assert due.at == datetime(2027, 2, 28, 23, 30, tzinfo=KOLKATA)
    assert due.collection_month == CollectionMonth(2027, 1)


def test_a_time_the_clocks_skip_is_the_moment_they_reach() -> None:
    # Clocks in New York went from 02:00 to 03:00 on 8 March 2026.
    skipped = Schedule(enabled=True, day=8, at=time(2, 30), time_zone="America/New_York")

    due = next_due(skipped, datetime(2026, 3, 1, tzinfo=UTC))

    assert due is not None
    assert due.at == datetime(2026, 3, 8, 7, 30, tzinfo=UTC)
    assert due.at.astimezone(NEW_YORK).hour == 3


def test_no_run_is_due_while_the_schedule_is_off() -> None:
    off = Schedule(enabled=False)

    assert next_due(off, datetime(2026, 9, 1, tzinfo=UTC)) is None
    assert last_due(off, datetime(2026, 9, 1, tzinfo=UTC)) is None


def test_the_run_due_last_is_found_from_any_moment_after_it() -> None:
    due = last_due(THIRD_AT_SIX, datetime(2026, 9, 29, 10, 0, tzinfo=UTC))

    assert due is not None
    assert due.at == datetime(2026, 9, 3, 6, 0, tzinfo=KOLKATA)
    assert due.collection_month == CollectionMonth(2026, 8)

    before = last_due(THIRD_AT_SIX, datetime(2026, 9, 2, tzinfo=UTC))
    assert before is not None
    assert before.collection_month == CollectionMonth(2026, 7)


@pytest.mark.parametrize("day", [0, 29, 31])
def test_a_day_that_some_month_lacks_is_refused(day: int) -> None:
    with pytest.raises(ScheduleRefused, match="from 1 to 28"):
        Schedule(enabled=True, day=day).check()


def test_a_time_zone_that_does_not_exist_is_refused() -> None:
    with pytest.raises(ScheduleRefused, match="not a time zone"):
        Schedule(enabled=True, time_zone="Mars/Olympus_Mons").check()


# The settings kept in the ledger


def test_settings_never_set_have_their_defaults_and_create_no_file(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite"

    settings = SettingsStore(path).read()

    assert settings == CollectionSettings()
    assert settings.schedule.enabled is False
    assert settings.schedule.time_zone == "Asia/Kolkata"
    assert not path.exists()


def test_a_change_is_kept_with_who_made_it_and_the_value_before(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "ledger.sqlite")
    at = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)

    made = store.change(CollectionSettings(THIRD_AT_SIX), "admin@nyayalabs.example", at)

    assert store.read() == CollectionSettings(THIRD_AT_SIX)
    # Only the switch changed: the day, the time and the zone were chosen as they were.
    assert [(c.name, c.value_before, c.value_after) for c in made] == [
        ("schedule.enabled", "off", "on")
    ]
    assert store.changes() == made
    assert store.last_changed(SCHEDULE_SETTINGS) == at


def test_setting_the_same_values_again_records_nothing(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "ledger.sqlite")
    at = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
    store.change(CollectionSettings(THIRD_AT_SIX), "admin@nyayalabs.example", at)

    again = store.change(CollectionSettings(THIRD_AT_SIX), "other@nyayalabs.example", at)

    assert again == []
    assert len(store.changes()) == 1


def test_each_change_is_listed_newest_first(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "ledger.sqlite")
    first = datetime(2026, 9, 1, tzinfo=UTC)
    later = datetime(2026, 9, 2, tzinfo=UTC)
    store.change(CollectionSettings(THIRD_AT_SIX), "a@nyayalabs.example", first)
    fifth = Schedule(enabled=True, day=5, at=time(6, 0), time_zone="Asia/Kolkata")

    store.change(CollectionSettings(fifth), "b@nyayalabs.example", later)

    assert [(c.name, c.value_before, c.value_after, c.person) for c in store.changes()] == [
        (SCHEDULE_DAY, "3", "5", "b@nyayalabs.example"),
        ("schedule.enabled", "off", "on", "a@nyayalabs.example"),
    ]
    assert store.last_changed(SCHEDULE_SETTINGS) == later
