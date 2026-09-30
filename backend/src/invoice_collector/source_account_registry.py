"""The source accounts the tool reads, and who connected each one and when.

Kept in its own table in the ledger's SQLite file, so a run reads every connected source
account with no list to keep elsewhere. The Ledger knows nothing of it; this module opens
the file itself for each read or write. Exactly one source account may be the owner
account. The sign-ins themselves are never kept here: they stay in the token folder.
"""

import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from invoice_collector.database import connect

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_accounts (
    address             TEXT NOT NULL PRIMARY KEY,
    is_owner            INTEGER NOT NULL DEFAULT 0,
    connected_by        TEXT NOT NULL,
    connected_at        TEXT NOT NULL,
    signed_in_at        TEXT,
    sign_in_fingerprint TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS one_owner_account ON source_accounts (is_owner)
    WHERE is_owner = 1;
CREATE TABLE IF NOT EXISTS source_account_changes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    address    TEXT NOT NULL,
    action     TEXT NOT NULL,
    person     TEXT NOT NULL,
    changed_at TEXT NOT NULL
);
"""

SourceAccountAction = Literal["connected", "renewed", "added", "removed", "made_owner"]


class OwnerAccountCannotBeRemoved(Exception):
    """The owner account is removed only after another source account is made the owner."""


@dataclass(frozen=True)
class RegisteredSourceAccount:
    address: str
    is_owner: bool
    connected_by: str
    connected_at: datetime
    # When the stored sign-in was issued through the dashboard, and a digest of it, so
    # that a sign-in replaced from the command line is not given this one's lifetime.
    signed_in_at: datetime | None
    sign_in_fingerprint: str | None


@dataclass(frozen=True)
class SourceAccountChange:
    address: str
    action: SourceAccountAction
    person: str
    changed_at: datetime


def normalise(address: str) -> str:
    return address.strip().lower()


def _time(text: str | None) -> datetime | None:
    return datetime.fromisoformat(text) if text is not None else None


class SourceAccountRegistry:
    def __init__(self, ledger_path: Path) -> None:
        self._path = ledger_path

    def _connect(self) -> sqlite3.Connection:
        db = connect(self._path)
        db.executescript(SCHEMA)
        return db

    def accounts(self) -> list[RegisteredSourceAccount]:
        """Every registered source account, the owner account first."""
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT address, is_owner, connected_by, connected_at, signed_in_at, "
                "sign_in_fingerprint FROM source_accounts ORDER BY is_owner DESC, address"
            ).fetchall()
        return [
            RegisteredSourceAccount(
                address=address,
                is_owner=bool(is_owner),
                connected_by=connected_by,
                connected_at=datetime.fromisoformat(connected_at),
                signed_in_at=_time(signed_in_at),
                sign_in_fingerprint=fingerprint,
            )
            for address, is_owner, connected_by, connected_at, signed_in_at, fingerprint in rows
        ]

    def addresses(self) -> list[str]:
        return [account.address for account in self.accounts()]

    def find(self, address: str) -> RegisteredSourceAccount | None:
        wanted = normalise(address)
        return next((each for each in self.accounts() if each.address == wanted), None)

    def signed_in(
        self,
        address: str,
        action: SourceAccountAction,
        person: str,
        at: datetime,
        *,
        signed_in_at: datetime | None,
        fingerprint: str | None,
        owner: bool = False,
    ) -> None:
        """Registers a source account, or records its new sign-in if it is registered.

        A source account connected as the owner account takes ownership from any other.
        """
        address = normalise(address)
        with closing(self._connect()) as db, db:
            db.execute(
                "INSERT INTO source_accounts (address, connected_by, connected_at, "
                "signed_in_at, sign_in_fingerprint) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (address) DO UPDATE SET signed_in_at = excluded.signed_in_at, "
                "sign_in_fingerprint = excluded.sign_in_fingerprint",
                (
                    address,
                    person,
                    at.isoformat(),
                    signed_in_at.isoformat() if signed_in_at is not None else None,
                    fingerprint,
                ),
            )
            _record(db, address, action, person, at)
            if owner:
                _make_owner(db, address, person, at)

    def make_owner(self, address: str, person: str, at: datetime) -> None:
        with closing(self._connect()) as db, db:
            _make_owner(db, normalise(address), person, at)

    def remove(self, address: str, person: str, at: datetime) -> None:
        address = normalise(address)
        with closing(self._connect()) as db, db:
            row = db.execute(
                "SELECT is_owner FROM source_accounts WHERE address = ?", (address,)
            ).fetchone()
            if row is not None and row[0]:
                raise OwnerAccountCannotBeRemoved(address)
            db.execute("DELETE FROM source_accounts WHERE address = ?", (address,))
            _record(db, address, "removed", person, at)

    def history(self) -> Sequence[SourceAccountChange]:
        """Every change to the source accounts, newest first."""
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT address, action, person, changed_at FROM source_account_changes "
                "ORDER BY changed_at DESC, id DESC"
            ).fetchall()
        return [
            SourceAccountChange(address, action, person, datetime.fromisoformat(changed_at))
            for address, action, person, changed_at in rows
        ]


def _make_owner(db: sqlite3.Connection, address: str, person: str, at: datetime) -> None:
    row = db.execute(
        "SELECT is_owner FROM source_accounts WHERE address = ?", (address,)
    ).fetchone()
    if row is None or row[0]:
        return
    db.execute("UPDATE source_accounts SET is_owner = 0 WHERE is_owner = 1")
    db.execute("UPDATE source_accounts SET is_owner = 1 WHERE address = ?", (address,))
    _record(db, address, "made_owner", person, at)


def _record(
    db: sqlite3.Connection, address: str, action: SourceAccountAction, person: str, at: datetime
) -> None:
    db.execute(
        "INSERT INTO source_account_changes (address, action, person, changed_at) "
        "VALUES (?, ?, ?, ?)",
        (address, action, person, at.isoformat()),
    )


def connected_source_accounts(ledger_path: Path) -> list[str]:
    """The address of every connected source account, which a run reads."""
    if not ledger_path.is_file():
        return []
    return SourceAccountRegistry(ledger_path).addresses()
