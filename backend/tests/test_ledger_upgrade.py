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


# Held documents as a version recorded them before the saved PDF of each was recorded.
HELD_BEFORE_SAVED_PDFS = """
CREATE TABLE pending_documents (
    source_account TEXT NOT NULL,
    message_id     TEXT NOT NULL,
    content_hash   TEXT NOT NULL,
    file_link      TEXT NOT NULL,
    document_type  TEXT NOT NULL,
    vendor         TEXT NOT NULL,
    invoice_date   TEXT NOT NULL,
    total          TEXT NOT NULL,
    currency       TEXT NOT NULL,
    inr_rate       TEXT,
    doubts         TEXT NOT NULL,
    read_again     INTEGER NOT NULL,
    vendor_as_read TEXT,
    PRIMARY KEY (source_account, message_id, content_hash)
);
INSERT INTO emails VALUES (
    'ops@nyayalabs.example', 'm-held-1', '2026-08', 'Figma <billing@figma.com>',
    'Your invoice is ready', '2026-08-21T06:05:00+00:00', 'needs_review', 'unsure'
);
INSERT INTO pending_documents VALUES (
    'ops@nyayalabs.example', 'm-held-1', 'hash-of-the-portal-link',
    'https://drive.example/2026-08/pending/2026-08_Figma_190.00-USD.pdf', 'invoice', 'Figma',
    '2026-08-21', '190.00', 'USD', NULL, '[]', 0, NULL
);
"""


def test_document_held_before_saved_pdfs_were_recorded_is_still_held(tmp_path: Path) -> None:
    path = first_version_ledger(tmp_path / "ledger.sqlite")
    db = sqlite3.connect(path)
    db.executescript(HELD_BEFORE_SAVED_PDFS)
    db.close()

    ledger = Ledger(path)
    [held] = ledger.pending(AUGUST)
    ledger.close()

    assert (held.message_id, held.content_hash) == ("m-held-1", "hash-of-the-portal-link")
    assert held.pdf_sha256 is None
