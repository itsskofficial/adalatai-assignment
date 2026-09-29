"""The record of every email examined and every billing document produced."""

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from invoice_collector.domain import Email, EmailState, Extraction

SCHEMA = """
CREATE TABLE IF NOT EXISTS emails (
    source_account TEXT NOT NULL,
    message_id     TEXT NOT NULL,
    sender         TEXT NOT NULL,
    subject        TEXT NOT NULL,
    received_at    TEXT NOT NULL,
    state          TEXT NOT NULL,
    reason         TEXT,
    PRIMARY KEY (source_account, message_id)
);
CREATE TABLE IF NOT EXISTS billing_documents (
    source_account TEXT NOT NULL,
    message_id     TEXT NOT NULL,
    file_link      TEXT NOT NULL,
    document_type  TEXT NOT NULL,
    vendor         TEXT NOT NULL,
    invoice_date   TEXT NOT NULL,
    total          TEXT NOT NULL,
    currency       TEXT NOT NULL,
    PRIMARY KEY (source_account, message_id),
    FOREIGN KEY (source_account, message_id) REFERENCES emails (source_account, message_id)
);
"""


@dataclass(frozen=True)
class ExaminedEmail:
    source_account: str
    message_id: str
    subject: str
    state: EmailState
    reason: str | None


@dataclass(frozen=True)
class SourceEmail:
    source_account: str
    message_id: str
    sender: str
    subject: str


class Ledger:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.executescript(SCHEMA)

    def record_email(self, email: Email, state: EmailState, reason: str | None = None) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO emails VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (source_account, message_id) "
                "DO UPDATE SET state = excluded.state, reason = excluded.reason",
                (
                    email.source_account,
                    email.message_id,
                    email.sender,
                    email.subject,
                    email.received_at.isoformat(),
                    state.value,
                    reason,
                ),
            )

    def record_billing_document(self, email: Email, extraction: Extraction, file_link: str) -> None:
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO billing_documents VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    email.source_account,
                    email.message_id,
                    file_link,
                    extraction.document_type,
                    extraction.vendor,
                    extraction.invoice_date.isoformat(),
                    str(extraction.total),
                    extraction.currency,
                ),
            )

    def examined_emails(self) -> list[ExaminedEmail]:
        rows = self._db.execute(
            "SELECT source_account, message_id, subject, state, reason FROM emails "
            "ORDER BY received_at, source_account, message_id"
        ).fetchall()
        return [ExaminedEmail(a, m, s, EmailState(st), r) for a, m, s, st, r in rows]

    def source_of(self, file_link: str) -> SourceEmail | None:
        row = self._db.execute(
            "SELECT e.source_account, e.message_id, e.sender, e.subject "
            "FROM billing_documents d JOIN emails e USING (source_account, message_id) "
            "WHERE d.file_link = ?",
            (file_link,),
        ).fetchone()
        return SourceEmail(*row) if row else None

    def close(self) -> None:
        self._db.close()
