"""Who may sign in to the dashboard, and what each person may do there.

The addresses in the INVOICE_COLLECTOR_ALLOWLIST setting are administrators set by the
installation: they can always sign in, and the dashboard cannot change or remove them. Anyone
else is on the people list, kept in the ledger's SQLite file beside the tables the Ledger
owns. The Ledger knows nothing of it; this module opens the file itself for each read or
write, as the dashboard's routes do.

Reading never creates the file, and nor does signing in: a dashboard that is only visited
leaves no ledger behind. Sign-ins and refused sign-ins are recorded once the ledger exists.
"""

import re
import sqlite3
from collections.abc import Generator, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, cast

from invoice_collector.api.settings import normalise
from invoice_collector.database import connect

Role = Literal["member", "administrator"]
ROLES: tuple[Role, ...] = ("member", "administrator")
PeopleAction = Literal["added", "removed", "role_changed"]
REFUSED_SHOWN = 50

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS people (
    address  TEXT NOT NULL PRIMARY KEY,
    role     TEXT NOT NULL,
    added_by TEXT NOT NULL,
    added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sign_ins (
    address           TEXT NOT NULL PRIMARY KEY,
    last_signed_in_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS refused_sign_ins (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    address      TEXT NOT NULL,
    attempted_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS people_changes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    address     TEXT NOT NULL,
    action      TEXT NOT NULL,
    role_before TEXT,
    role_after  TEXT,
    person      TEXT NOT NULL,
    changed_at  TEXT NOT NULL
);
"""


class PersonRefused(Exception):
    """A change to the people list breaks a rule. The message says which, in plain words."""

    def __init__(self, message: str, *, status: Literal[404, 409, 422]) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class PersonEntry:
    address: str
    role: Role
    set_by_installation: bool
    added_by: str | None
    added_at: datetime | None
    last_signed_in_at: datetime | None


@dataclass(frozen=True)
class PeopleChange:
    address: str
    action: PeopleAction
    role_before: Role | None
    role_after: Role | None
    person: str
    changed_at: datetime


@dataclass(frozen=True)
class RefusedSignIn:
    address: str
    attempted_at: datetime


def email_address(text: str) -> str:
    """The address in lower case, or PersonRefused when it is not an email address."""
    address = normalise(text)
    if not EMAIL_PATTERN.match(address):
        raise PersonRefused(
            "The address must be an email address, such as name@example.com", status=422
        )
    return address


def role(text: str) -> Role:
    if text not in ROLES:
        raise PersonRefused("The role must be member or administrator", status=422)
    return text


def _time(text: str | None) -> datetime | None:
    return datetime.fromisoformat(text) if text is not None else None


class People:
    """The people who may sign in: those set by the installation, and the people list."""

    def __init__(self, ledger_path: Path, installation: frozenset[str]) -> None:
        self._path = ledger_path
        self._installation = frozenset(normalise(address) for address in installation)

    @contextmanager
    def _writing(self) -> Generator[sqlite3.Connection]:
        with closing(connect(self._path)) as db, db:
            db.executescript(SCHEMA)
            yield db

    @contextmanager
    def _if_ledger_exists(self) -> Generator[sqlite3.Connection | None]:
        """The file opened, or None when there is no ledger yet."""
        if not self._path.is_file():
            yield None
            return
        with closing(connect(self._path)) as db, db:
            db.executescript(SCHEMA)
            yield db

    def _listed_role(self, db: sqlite3.Connection, address: str) -> Role | None:
        row = db.execute("SELECT role FROM people WHERE address = ?", (address,)).fetchone()
        return cast(Role, row[0]) if row is not None else None

    def role_of(self, email: str) -> Role | None:
        """The person's role, or None when they may not sign in."""
        address = normalise(email)
        if address in self._installation:
            return "administrator"
        with self._if_ledger_exists() as db:
            return self._listed_role(db, address) if db is not None else None

    def nobody_could_sign_in(self) -> bool:
        if self._installation:
            return False
        with self._if_ledger_exists() as db:
            if db is None:
                return True
            return db.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 0

    def everyone(self) -> Sequence[PersonEntry]:
        """Those set by the installation first, then the people list, each in address order."""
        with self._if_ledger_exists() as db:
            listed = (
                db.execute(
                    "SELECT address, role, added_by, added_at FROM people ORDER BY address"
                ).fetchall()
                if db is not None
                else []
            )
            signed_in: dict[str, str] = (
                dict(db.execute("SELECT address, last_signed_in_at FROM sign_ins").fetchall())
                if db is not None
                else {}
            )
        installation = [
            PersonEntry(
                address=address,
                role="administrator",
                set_by_installation=True,
                added_by=None,
                added_at=None,
                last_signed_in_at=_time(signed_in.get(address)),
            )
            for address in sorted(self._installation)
        ]
        return installation + [
            PersonEntry(
                address=address,
                role=cast(Role, listed_role),
                set_by_installation=False,
                added_by=added_by,
                added_at=_time(added_at),
                last_signed_in_at=_time(signed_in.get(address)),
            )
            for address, listed_role, added_by, added_at in listed
            if address not in self._installation
        ]

    def _refuse_installation(self, address: str) -> None:
        if address in self._installation:
            raise PersonRefused(
                f"{address} is set by the installation and cannot be changed in the dashboard",
                status=409,
            )

    def _current(self, db: sqlite3.Connection, address: str) -> Role:
        current = self._listed_role(db, address)
        if current is None:
            raise PersonRefused(f"{address} is not on the list", status=404)
        return current

    def _refuse_leaving_no_administrator(self, db: sqlite3.Connection, address: str) -> None:
        if self._installation:
            return
        others = db.execute(
            "SELECT COUNT(*) FROM people WHERE role = 'administrator' AND address != ?",
            (address,),
        ).fetchone()[0]
        if others == 0:
            raise PersonRefused(
                "That would leave nobody who can manage who may sign in", status=409
            )

    def _record(
        self,
        db: sqlite3.Connection,
        address: str,
        action: PeopleAction,
        before: Role | None,
        after: Role | None,
        person: str,
        at: datetime,
    ) -> None:
        db.execute(
            "INSERT INTO people_changes "
            "(address, action, role_before, role_after, person, changed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (address, action, before, after, person, at.isoformat()),
        )

    def add(self, address: str, role: Role, *, by: str, at: datetime) -> PersonEntry:
        address = email_address(address)
        with self._writing() as db:
            if address in self._installation or self._listed_role(db, address) is not None:
                raise PersonRefused(f"{address} is already on the list", status=409)
            db.execute(
                "INSERT INTO people (address, role, added_by, added_at) VALUES (?, ?, ?, ?)",
                (address, role, by, at.isoformat()),
            )
            self._record(db, address, "added", None, role, by, at)
        return self._entry(address)

    def change_role(self, address: str, role: Role, *, by: str, at: datetime) -> PersonEntry:
        address = normalise(address)
        self._refuse_installation(address)
        with self._writing() as db:
            before = self._current(db, address)
            if before != role:
                if before == "administrator":
                    self._refuse_leaving_no_administrator(db, address)
                db.execute("UPDATE people SET role = ? WHERE address = ?", (role, address))
                self._record(db, address, "role_changed", before, role, by, at)
        return self._entry(address)

    def remove(self, address: str, *, by: str, at: datetime) -> None:
        address = normalise(address)
        self._refuse_installation(address)
        with self._writing() as db:
            before = self._current(db, address)
            if before == "administrator":
                self._refuse_leaving_no_administrator(db, address)
            db.execute("DELETE FROM people WHERE address = ?", (address,))
            self._record(db, address, "removed", before, None, by, at)

    def _entry(self, address: str) -> PersonEntry:
        return next(entry for entry in self.everyone() if entry.address == address)

    def history(self) -> Sequence[PeopleChange]:
        """Every addition, removal and change of role, newest first."""
        with self._if_ledger_exists() as db:
            rows = (
                db.execute(
                    "SELECT address, action, role_before, role_after, person, changed_at "
                    "FROM people_changes ORDER BY changed_at DESC, id DESC"
                ).fetchall()
                if db is not None
                else []
            )
        return [
            PeopleChange(
                address=address,
                action=action,
                role_before=before,
                role_after=after,
                person=person,
                changed_at=datetime.fromisoformat(changed_at),
            )
            for address, action, before, after, person, changed_at in rows
        ]

    def record_sign_in(self, email: str, at: datetime) -> None:
        with self._if_ledger_exists() as db:
            if db is not None:
                db.execute(
                    "INSERT INTO sign_ins (address, last_signed_in_at) VALUES (?, ?) "
                    "ON CONFLICT (address) DO UPDATE "
                    "SET last_signed_in_at = excluded.last_signed_in_at",
                    (normalise(email), at.isoformat()),
                )

    def record_refusal(self, email: str, at: datetime) -> None:
        with self._if_ledger_exists() as db:
            if db is not None:
                db.execute(
                    "INSERT INTO refused_sign_ins (address, attempted_at) VALUES (?, ?)",
                    (normalise(email), at.isoformat()),
                )

    def refusals(self) -> Sequence[RefusedSignIn]:
        """The latest refused sign-ins, newest first."""
        with self._if_ledger_exists() as db:
            rows = (
                db.execute(
                    "SELECT address, attempted_at FROM refused_sign_ins "
                    "ORDER BY attempted_at DESC, id DESC LIMIT ?",
                    (REFUSED_SHOWN,),
                ).fetchall()
                if db is not None
                else []
            )
        return [
            RefusedSignIn(address=address, attempted_at=datetime.fromisoformat(attempted_at))
            for address, attempted_at in rows
        ]
