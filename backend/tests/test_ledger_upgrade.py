"""A ledger made by an earlier version keeps working."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from invoice_collector.domain import CollectionMonth, Email, EmailState, InvoiceFormat
from invoice_collector.ledger import Ledger

AUGUST = CollectionMonth(2026, 8)

# The ledger as the first version created it, before invoice formats were recorded.
FIRST_VERSION = """
CREATE TABLE emails (
    source_account   TEXT NOT NULL,
    message_id       TEXT NOT NULL,
    collection_month TEXT NOT NULL,
    sender           TEXT NOT NULL,
    subject          TEXT NOT NULL,
    received_at      TEXT NOT NULL,
    state            TEXT NOT NULL,
    reason           TEXT,
    PRIMARY KEY (source_account, message_id)
);
INSERT INTO emails VALUES (
    'ops@nyayalabs.example', 'm-old-1', '2026-08', 'Zoom <billing@zoom.us>',
    'Zoom payment confirmation', '2026-08-09T06:00:00+00:00', 'skipped', 'no PDF attachment'
);
"""


def email(message_id: str) -> Email:
    return Email(
        source_account="ops@nyayalabs.example",
        message_id=message_id,
        sender="Figma <billing@figma.com>",
        subject="Your invoice is ready",
        received_at=datetime(2026, 8, 21, 6, 5, tzinfo=UTC),
    )


def first_version_ledger(path: Path) -> Path:
    db = sqlite3.connect(path)
    db.executescript(FIRST_VERSION)
    db.close()
    return path


def test_ledger_from_the_first_version_accepts_new_outcomes(tmp_path: Path) -> None:
    ledger = Ledger(first_version_ledger(tmp_path / "ledger.sqlite"))

    ledger.record(
        AUGUST,
        email("m-new-1"),
        EmailState.NEEDS_REVIEW,
        reason="manual download needed",
        invoice_format=InvoiceFormat.PORTAL_LINK,
        portal_link="https://linear.example/settings/billing",
    )

    outcomes = {e.message_id: e for e in ledger.examined_emails(AUGUST)}
    ledger.close()
    assert outcomes["m-new-1"].invoice_format is InvoiceFormat.PORTAL_LINK
    assert outcomes["m-new-1"].portal_link == "https://linear.example/settings/billing"


def test_what_the_first_version_recorded_is_kept(tmp_path: Path) -> None:
    ledger = Ledger(first_version_ledger(tmp_path / "ledger.sqlite"))

    outcomes = {e.message_id: e for e in ledger.examined_emails(AUGUST)}
    ledger.close()

    assert outcomes["m-old-1"].state is EmailState.SKIPPED
    assert outcomes["m-old-1"].reason == "no PDF attachment"
    assert outcomes["m-old-1"].invoice_format is None


def test_opening_a_ledger_twice_changes_nothing(tmp_path: Path) -> None:
    path = first_version_ledger(tmp_path / "ledger.sqlite")
    Ledger(path).close()

    ledger = Ledger(path)
    outcomes = ledger.examined_emails(AUGUST)
    ledger.close()

    assert [e.message_id for e in outcomes] == ["m-old-1"]
