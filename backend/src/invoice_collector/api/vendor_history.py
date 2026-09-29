"""Who changed the vendor list, when, and what the entry held before and after.

Kept in its own table in the ledger's SQLite file. The Ledger knows nothing of it; this
module opens the file itself for each read or write, as the dashboard's routes do.
"""

import json
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from invoice_collector.database import connect
from invoice_collector.domain import ExpectedVendor

VendorAction = Literal["added", "edited", "removed", "accepted", "ignored", "restored"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS vendor_changes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor     TEXT NOT NULL,
    action     TEXT NOT NULL,
    person     TEXT NOT NULL,
    changed_at TEXT NOT NULL,
    before     TEXT,
    after      TEXT
);
CREATE INDEX IF NOT EXISTS vendor_changes_by_vendor ON vendor_changes (vendor);
"""


@dataclass(frozen=True)
class VendorChangeRecord:
    vendor: str
    action: VendorAction
    person: str
    changed_at: datetime
    before: dict[str, Any] | None
    after: dict[str, Any] | None


def as_values(vendor: ExpectedVendor) -> dict[str, Any]:
    """A vendor list entry as plain values, the way it is kept in the history."""
    return {
        "vendor": vendor.vendor,
        "source_account": vendor.source_account,
        "billing_cycle": vendor.billing_cycle,
        "renewal_month": vendor.renewal_month,
        "usual_amount": str(vendor.usual_amount) if vendor.usual_amount is not None else None,
        "currency": vendor.currency,
        "status": vendor.status,
    }


def _json(vendor: ExpectedVendor | None) -> str | None:
    return json.dumps(as_values(vendor)) if vendor is not None else None


def _values(text: str | None) -> dict[str, Any] | None:
    return json.loads(text) if text is not None else None


class VendorHistory:
    def __init__(self, ledger_path: Path) -> None:
        self._path = ledger_path

    def _connect(self) -> sqlite3.Connection:
        db = connect(self._path)
        db.executescript(SCHEMA)
        return db

    def record(
        self,
        action: VendorAction,
        person: str,
        changed_at: datetime,
        before: ExpectedVendor | None,
        after: ExpectedVendor | None,
    ) -> None:
        """Records one change. A rename carries the vendor's earlier history to its new name."""
        named = after or before
        if named is None:
            raise ValueError("A change needs the entry before it or after it")
        with closing(self._connect()) as db, db:
            if before is not None and after is not None and before.vendor != after.vendor:
                db.execute(
                    "UPDATE vendor_changes SET vendor = ? WHERE vendor = ?",
                    (after.vendor, before.vendor),
                )
            db.execute(
                "INSERT INTO vendor_changes (vendor, action, person, changed_at, before, after) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    named.vendor,
                    action,
                    person,
                    changed_at.isoformat(),
                    _json(before),
                    _json(after),
                ),
            )

    def of(self, vendor: str) -> Sequence[VendorChangeRecord]:
        """Every change to a vendor's entry, newest first."""
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT vendor, action, person, changed_at, before, after FROM vendor_changes "
                "WHERE vendor = ? ORDER BY changed_at DESC, id DESC",
                (vendor,),
            ).fetchall()
        return [
            VendorChangeRecord(
                vendor=name,
                action=action,
                person=person,
                changed_at=datetime.fromisoformat(changed_at),
                before=_values(before),
                after=_values(after),
            )
            for name, action, person, changed_at, before, after in rows
        ]
