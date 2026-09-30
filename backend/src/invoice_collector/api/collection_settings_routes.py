"""What the Settings screen shows and changes: the schedule, and the Drive folder.

The settings are kept in the ledger (collection_settings.py). Everyone signed in sees them;
administrators change them. A change to the schedule is told to the runner, which works out
its next run again. When the runner cannot be reached the change is still kept, and the
runner reads it when it starts.

Secrets are not settings of this screen: they stay in the environment.
"""

from collections.abc import Callable
from datetime import datetime, time
from typing import Annotated, Protocol

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from invoice_collector.api.people import People
from invoice_collector.collection_settings import (
    SCHEDULE_SETTINGS,
    CollectionSettings,
    SettingChange,
    SettingRefused,
    SettingsStore,
    drive_folder_name,
)
from invoice_collector.run_starter import RunnerUnreachable
from invoice_collector.schedule import LAST_DAY, Schedule, ScheduleRefused, next_due

PersonDependency = Callable[..., str]
TIME_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
CHANGES_SHOWN = 20


class ScheduleKeeper(Protocol):
    """The runner, which keeps the schedule."""

    def schedule_changed(self) -> None:
        """Tells it the schedule changed. Raises RunnerUnreachable."""
        ...


class ScheduleSettings(BaseModel):
    enabled: bool
    day: Annotated[int, Field(ge=1, le=LAST_DAY)]
    # The time of day, HH:MM, in the time zone.
    time: Annotated[str, Field(pattern=TIME_PATTERN)]
    time_zone: Annotated[str, Field(min_length=1)]


class NextRun(BaseModel):
    # When it is due, in the schedule's time zone.
    due_at: str
    collection_month: str


class SettingChangeView(BaseModel):
    name: str
    value_before: str | None
    value_after: str
    person: str
    changed_at: str


class SettingsView(BaseModel):
    schedule: ScheduleSettings
    drive_folder: str
    # None while the schedule is off.
    next_run: NextRun | None
    # Whether the person may change them: administrators may.
    can_change: bool
    # Whether a runner keeps the schedule for this dashboard.
    runner: bool
    changes: list[SettingChangeView]


class SettingsChange(BaseModel):
    schedule: ScheduleSettings
    drive_folder: str


class SettingsSaved(SettingsView):
    # What became of telling the runner, when the schedule changed.
    runner_notice: str | None


def _change_view(change: SettingChange) -> SettingChangeView:
    return SettingChangeView(
        name=change.name,
        value_before=change.value_before,
        value_after=change.value_after,
        person=change.person,
        changed_at=change.changed_at.isoformat(),
    )


def _schedule(asked: ScheduleSettings) -> Schedule:
    return Schedule(
        enabled=asked.enabled,
        day=asked.day,
        at=time.fromisoformat(asked.time),
        time_zone=asked.time_zone.strip(),
    )


def collection_settings_routes(
    store: SettingsStore,
    people: People,
    keeper: ScheduleKeeper | None,
    signed_in_person: PersonDependency,
    now: Callable[[], datetime],
) -> APIRouter:
    router = APIRouter(prefix="/settings")

    def view(person: str) -> SettingsView:
        settings = store.read()
        schedule = settings.schedule
        due = next_due(schedule, now())
        return SettingsView(
            schedule=ScheduleSettings(
                enabled=schedule.enabled,
                day=schedule.day,
                time=schedule.at.strftime("%H:%M"),
                time_zone=schedule.time_zone,
            ),
            drive_folder=settings.drive_folder,
            next_run=(
                NextRun(
                    due_at=due.at.astimezone(schedule.zone).isoformat(),
                    collection_month=str(due.collection_month),
                )
                if due is not None
                else None
            ),
            can_change=people.role_of(person) == "administrator",
            runner=keeper is not None,
            changes=[_change_view(change) for change in store.changes(CHANGES_SHOWN)],
        )

    def tell_the_runner() -> str:
        if keeper is None:
            return (
                "No runner service is set up for this dashboard, so nothing runs on the "
                "schedule until one is started with this ledger. It reads the schedule when "
                "it starts."
            )
        try:
            keeper.schedule_changed()
        except RunnerUnreachable as unreachable:
            return (
                f"The change is saved. {unreachable} The runner will pick up the new "
                "schedule when it starts."
            )
        return "The runner has the new schedule."

    @router.get("")
    def settings(  # pyright: ignore[reportUnusedFunction]
        person: Annotated[str, Depends(signed_in_person)],
    ) -> SettingsView:
        return view(person)

    @router.put("")
    def change(  # pyright: ignore[reportUnusedFunction]
        asked: SettingsChange,
        person: Annotated[str, Depends(signed_in_person)],
    ) -> SettingsSaved:
        if people.role_of(person) != "administrator":
            raise HTTPException(
                status_code=403, detail="Only an administrator can change the settings"
            )
        schedule = _schedule(asked.schedule)
        try:
            schedule.check()
            folder = drive_folder_name(asked.drive_folder)
        except (ScheduleRefused, SettingRefused) as refused:
            raise HTTPException(status_code=422, detail=str(refused)) from None
        made = store.change(CollectionSettings(schedule, folder), person, now())
        notice = (
            tell_the_runner() if any(change.name in SCHEDULE_SETTINGS for change in made) else None
        )
        return SettingsSaved(**view(person).model_dump(), runner_notice=notice)

    return router
