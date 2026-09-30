"""The settings a person chooses in the dashboard, kept in the ledger's file.

Only choices about collecting are kept here: the schedule, and the folder in the owner
account's Drive the PDFs and sheets go to. Secrets (API keys, the OAuth client, the Slack
webhook, the runner's shared secret) stay in the environment, set by whoever deploys the
tool.

Each setting is a row with who last changed it and when, and every change is kept with the
value before and after, as other changes made in the dashboard are. A setting that has never
been set has its default. Reading never creates the file.
"""

import sqlite3
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import datetime, time
from pathlib import Path

from invoice_collector.database import connect
from invoice_collector.drive_archive import DEFAULT_ROOT_FOLDER
from invoice_collector.schedule import Schedule

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    name       TEXT NOT NULL PRIMARY KEY,
    value      TEXT NOT NULL,
    changed_by TEXT NOT NULL,
    changed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings_changes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    value_before TEXT,
    value_after  TEXT NOT NULL,
    person       TEXT NOT NULL,
    changed_at   TEXT NOT NULL
);
"""

SCHEDULE_ENABLED = "schedule.enabled"
SCHEDULE_DAY = "schedule.day"
SCHEDULE_TIME = "schedule.time"
SCHEDULE_TIME_ZONE = "schedule.time_zone"
SCHEDULE_SETTINGS = (SCHEDULE_ENABLED, SCHEDULE_DAY, SCHEDULE_TIME, SCHEDULE_TIME_ZONE)
DRIVE_FOLDER = "drive.folder"
# A folder name longer than this is a mistake, not a choice.
LONGEST_FOLDER_NAME = 100


class SettingRefused(Exception):
    """A setting cannot be kept as given. The message says why in plain words."""


def drive_folder_name(text: str) -> str:
    """The name of the Drive folder, trimmed, or SettingRefused when it cannot be one."""
    name = " ".join(text.split())
    if not name:
        raise SettingRefused("The Drive folder needs a name.")
    if len(name) > LONGEST_FOLDER_NAME:
        raise SettingRefused(
            f"The Drive folder's name must be at most {LONGEST_FOLDER_NAME} characters."
        )
    if "/" in name:
        raise SettingRefused(
            "The Drive folder's name cannot hold a /, which would read as a folder inside another."
        )
    return name


@dataclass(frozen=True)
class CollectionSettings:
    schedule: Schedule = Schedule()
    # The folder in the owner account's Drive, at the top of My Drive, where PDFs and the
    # summary sheets go. Changing it moves nothing already filed.
    drive_folder: str = DEFAULT_ROOT_FOLDER


@dataclass(frozen=True)
class SettingChange:
    name: str
    value_before: str | None
    value_after: str
    person: str
    changed_at: datetime


def _values(settings: CollectionSettings) -> dict[str, str]:
    schedule = settings.schedule
    return {
        SCHEDULE_ENABLED: "on" if schedule.enabled else "off",
        SCHEDULE_DAY: str(schedule.day),
        SCHEDULE_TIME: schedule.at.strftime("%H:%M"),
        SCHEDULE_TIME_ZONE: schedule.time_zone,
        DRIVE_FOLDER: settings.drive_folder,
    }


def _settings(values: Mapping[str, str]) -> CollectionSettings:
    default = CollectionSettings()
    schedule = default.schedule
    if SCHEDULE_ENABLED in values:
        schedule = replace(schedule, enabled=values[SCHEDULE_ENABLED] == "on")
    if SCHEDULE_DAY in values:
        schedule = replace(schedule, day=int(values[SCHEDULE_DAY]))
    if SCHEDULE_TIME in values:
        schedule = replace(schedule, at=time.fromisoformat(values[SCHEDULE_TIME]))
    if SCHEDULE_TIME_ZONE in values:
        schedule = replace(schedule, time_zone=values[SCHEDULE_TIME_ZONE])
    return replace(
        default, schedule=schedule, drive_folder=values.get(DRIVE_FOLDER, default.drive_folder)
    )


class SettingsStore:
    def __init__(self, ledger_path: Path) -> None:
        self._path = ledger_path

    def _connect(self) -> sqlite3.Connection:
        db = connect(self._path)
        db.executescript(SCHEMA)
        return db

    def _stored(self) -> dict[str, tuple[str, str, datetime]]:
        if not self._path.is_file():
            return {}
        with closing(self._connect()) as db:
            rows = db.execute("SELECT name, value, changed_by, changed_at FROM settings").fetchall()
        return {
            name: (value, changed_by, datetime.fromisoformat(changed_at))
            for name, value, changed_by, changed_at in rows
        }

    def read(self) -> CollectionSettings:
        """The settings, each at its default until it is first set."""
        return _settings({name: value for name, (value, _, _) in self._stored().items()})

    def last_changed(self, names: tuple[str, ...]) -> datetime | None:
        """When any of these settings was last changed, or None when none has been set."""
        times = [at for name, (_, _, at) in self._stored().items() if name in names]
        return max(times, default=None)

    def change(
        self, settings: CollectionSettings, person: str, at: datetime
    ) -> list[SettingChange]:
        """Keeps the settings, recording each one that changed. Gives the changes made."""
        wanted = _values(settings)
        changes: list[SettingChange] = []
        with closing(self._connect()) as db, db:
            stored = dict(db.execute("SELECT name, value FROM settings").fetchall())
            current = _values(_settings(stored))
            for name, value in wanted.items():
                # What was in effect before, its default when it had never been set.
                before = current[name]
                if before == value:
                    continue
                db.execute(
                    "INSERT OR REPLACE INTO settings (name, value, changed_by, changed_at) "
                    "VALUES (?, ?, ?, ?)",
                    (name, value, person, at.isoformat()),
                )
                db.execute(
                    "INSERT INTO settings_changes (name, value_before, value_after, person, "
                    "changed_at) VALUES (?, ?, ?, ?, ?)",
                    (name, before, value, person, at.isoformat()),
                )
                changes.append(SettingChange(name, before, value, person, at))
        return changes

    def changes(self, limit: int = 50) -> list[SettingChange]:
        """The latest changes, newest first."""
        if not self._path.is_file():
            return []
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT name, value_before, value_after, person, changed_at "
                "FROM settings_changes ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            SettingChange(name, before, after, person, datetime.fromisoformat(changed_at))
            for name, before, after, person, changed_at in rows
        ]
