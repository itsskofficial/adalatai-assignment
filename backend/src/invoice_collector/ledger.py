"""The record of every email examined and every billing document produced."""

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from invoice_collector.domain import (
    BillingSignal,
    CollectionMonth,
    Email,
    EmailState,
    Extraction,
    InvoiceFormat,
    SummaryRow,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS emails (
    source_account   TEXT NOT NULL,
    message_id       TEXT NOT NULL,
    collection_month TEXT NOT NULL,
    sender           TEXT NOT NULL,
    subject          TEXT NOT NULL,
    received_at      TEXT NOT NULL,
    state            TEXT NOT NULL,
    reason           TEXT,
    invoice_format   TEXT,
    portal_link      TEXT,
    PRIMARY KEY (source_account, message_id)
);
CREATE TABLE IF NOT EXISTS billing_documents (
    source_account TEXT NOT NULL,
    message_id     TEXT NOT NULL,
    content_hash   TEXT NOT NULL,
    file_link      TEXT NOT NULL,
    document_type  TEXT NOT NULL,
    vendor         TEXT NOT NULL,
    invoice_date   TEXT NOT NULL,
    total          TEXT NOT NULL,
    currency       TEXT NOT NULL,
    PRIMARY KEY (source_account, message_id, content_hash),
    FOREIGN KEY (source_account, message_id) REFERENCES emails (source_account, message_id)
);
CREATE TABLE IF NOT EXISTS billing_signals (
    source_account TEXT NOT NULL,
    message_id     TEXT NOT NULL,
    kind           TEXT NOT NULL,
    vendor         TEXT,
    PRIMARY KEY (source_account, message_id),
    FOREIGN KEY (source_account, message_id) REFERENCES emails (source_account, message_id)
);
"""

# Columns added since a table was first created. SCHEMA creates new ledgers with them;
# these bring a ledger made by an earlier version up to date.
ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "emails": {"invoice_format": "TEXT", "portal_link": "TEXT"},
}


@dataclass(frozen=True)
class CollectedDocument:
    content_hash: str
    extraction: Extraction
    file_link: str


@dataclass(frozen=True)
class ExaminedEmail:
    source_account: str
    message_id: str
    subject: str
    state: EmailState
    reason: str | None
    invoice_format: InvoiceFormat | None
    portal_link: str | None


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
        self._add_missing_columns()

    def _add_missing_columns(self) -> None:
        with self._db:
            for table, columns in ADDED_COLUMNS.items():
                present = {row[1] for row in self._db.execute(f"PRAGMA table_info({table})")}
                for column, kind in columns.items():
                    if column not in present:
                        self._db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")

    def record(
        self,
        month: CollectionMonth,
        email: Email,
        state: EmailState,
        *,
        reason: str | None = None,
        invoice_format: InvoiceFormat | None = None,
        portal_link: str | None = None,
        documents: tuple[CollectedDocument, ...] = (),
        signal: BillingSignal | None = None,
    ) -> None:
        """Records the outcome for one email, replacing any earlier outcome for it."""
        key = (email.source_account, email.message_id)
        with self._db:
            self._db.execute(
                "DELETE FROM billing_documents WHERE source_account = ? AND message_id = ?", key
            )
            self._db.execute(
                "DELETE FROM billing_signals WHERE source_account = ? AND message_id = ?", key
            )
            self._db.execute(
                # Columns are named, since a ledger brought up to date holds them in
                # a different order from a new one.
                "INSERT OR REPLACE INTO emails (source_account, message_id, collection_month, "
                "sender, subject, received_at, state, reason, invoice_format, portal_link) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    *key,
                    str(month),
                    email.sender,
                    email.subject,
                    email.received_at.isoformat(),
                    state.value,
                    reason,
                    invoice_format.value if invoice_format else None,
                    portal_link,
                ),
            )
            self._db.executemany(
                "INSERT INTO billing_documents (source_account, message_id, content_hash, "
                "file_link, document_type, vendor, invoice_date, total, currency) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        *key,
                        d.content_hash,
                        d.file_link,
                        d.extraction.document_type,
                        d.extraction.vendor,
                        d.extraction.invoice_date.isoformat(),
                        str(d.extraction.total),
                        d.extraction.currency,
                    )
                    for d in documents
                ],
            )
            if signal:
                self._db.execute(
                    "INSERT INTO billing_signals VALUES (?, ?, ?, ?)",
                    (*key, signal.kind, signal.vendor),
                )

    def examined_emails(self, month: CollectionMonth) -> list[ExaminedEmail]:
        rows = self._db.execute(
            "SELECT source_account, message_id, subject, state, reason, invoice_format, "
            "portal_link FROM emails WHERE collection_month = ? "
            "ORDER BY received_at, source_account, message_id",
            (str(month),),
        ).fetchall()
        return [
            ExaminedEmail(
                source_account=account,
                message_id=message_id,
                subject=subject,
                state=EmailState(state),
                reason=reason,
                invoice_format=InvoiceFormat(fmt) if fmt else None,
                portal_link=portal_link,
            )
            for account, message_id, subject, state, reason, fmt, portal_link in rows
        ]

    def summary(self, month: CollectionMonth) -> list[SummaryRow]:
        rows = self._db.execute(
            "SELECT d.vendor, d.document_type, d.invoice_date, d.total, d.currency, "
            "d.source_account, d.file_link "
            "FROM billing_documents d JOIN emails e USING (source_account, message_id) "
            "WHERE e.collection_month = ? AND e.state = ? "
            "ORDER BY d.invoice_date, d.vendor, d.source_account, d.file_link",
            (str(month), EmailState.COLLECTED.value),
        ).fetchall()
        return [
            SummaryRow(
                vendor=vendor,
                document_type=document_type,
                invoice_date=date.fromisoformat(invoice_date),
                total=Decimal(total),
                currency=currency,
                source_account=account,
                file_link=file_link,
            )
            for vendor, document_type, invoice_date, total, currency, account, file_link in rows
        ]

    def billing_signals(self, month: CollectionMonth) -> list[BillingSignal]:
        rows = self._db.execute(
            "SELECT s.kind, s.vendor, e.source_account, e.message_id, e.subject, e.received_at "
            "FROM billing_signals s JOIN emails e USING (source_account, message_id) "
            "WHERE e.collection_month = ? ORDER BY e.received_at, e.source_account",
            (str(month),),
        ).fetchall()
        return [
            BillingSignal(
                kind=kind,
                vendor=vendor,
                source_account=account,
                message_id=message_id,
                subject=subject,
                received_at=datetime.fromisoformat(received_at),
            )
            for kind, vendor, account, message_id, subject, received_at in rows
        ]

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
