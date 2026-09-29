"""The record of every email examined and every billing document produced."""

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from invoice_collector.domain import (
    BillingSignal,
    CollectionMonth,
    Doubt,
    Email,
    EmailState,
    ExpectedVendor,
    Extraction,
    InvoiceFormat,
    Sync,
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
    inr_rate       TEXT,
    PRIMARY KEY (source_account, message_id, content_hash),
    FOREIGN KEY (source_account, message_id) REFERENCES emails (source_account, message_id)
);
CREATE INDEX IF NOT EXISTS billing_documents_by_hash ON billing_documents (content_hash);
CREATE TABLE IF NOT EXISTS billing_signals (
    source_account TEXT NOT NULL,
    message_id     TEXT NOT NULL,
    kind           TEXT NOT NULL,
    vendor         TEXT,
    PRIMARY KEY (source_account, message_id),
    FOREIGN KEY (source_account, message_id) REFERENCES emails (source_account, message_id)
);
CREATE TABLE IF NOT EXISTS pending_documents (
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
    PRIMARY KEY (source_account, message_id, content_hash),
    FOREIGN KEY (source_account, message_id) REFERENCES emails (source_account, message_id)
);
CREATE INDEX IF NOT EXISTS pending_documents_by_hash ON pending_documents (content_hash);
CREATE TABLE IF NOT EXISTS expected_vendors (
    vendor         TEXT NOT NULL PRIMARY KEY,
    source_account TEXT,
    billing_cycle  TEXT NOT NULL,
    renewal_month  INTEGER,
    usual_amount   TEXT,
    currency       TEXT,
    status         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS syncs (
    collection_month TEXT NOT NULL,
    source_account   TEXT NOT NULL,
    succeeded        INTEGER NOT NULL,
    reason           TEXT,
    PRIMARY KEY (collection_month, source_account)
);
"""

# Columns added since a table was first created. SCHEMA creates new ledgers with them;
# these bring a ledger made by an earlier version up to date.
ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "emails": {"invoice_format": "TEXT", "portal_link": "TEXT"},
    "billing_documents": {"inr_rate": "TEXT"},
}

_DOCUMENT_COLUMNS = (
    "d.source_account, d.message_id, d.content_hash, d.file_link, d.document_type, d.vendor, "
    "d.invoice_date, d.total, d.currency, d.inr_rate"
)

_PENDING_COLUMNS = f"{_DOCUMENT_COLUMNS}, d.doubts, d.read_again"


@dataclass(frozen=True)
class CollectedDocument:
    """A billing document as the run hands it to the ledger.

    The content hash identifies the document by what it was made from: the bytes of an
    attached PDF, the body of an email, or the address of a portal page. A PDF rendered
    twice from the same body differs byte for byte, so its own bytes cannot identify it.
    """

    content_hash: str
    extraction: Extraction
    file_link: str
    inr_rate: Decimal | None = None


@dataclass(frozen=True)
class PendingDocument:
    """A billing document held for a person to confirm, with the reasons it is held."""

    content_hash: str
    extraction: Extraction
    file_link: str
    doubts: tuple[Doubt, ...]
    inr_rate: Decimal | None = None
    # Whether a stronger model read the document after the first reading was doubted.
    read_again: bool = False
    source_account: str = ""
    message_id: str = ""


@dataclass(frozen=True)
class DocumentRecord:
    source_account: str
    message_id: str
    content_hash: str
    extraction: Extraction
    file_link: str
    inr_rate: Decimal | None


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


def _document(row: tuple[Any, ...]) -> DocumentRecord:
    account, message_id, content_hash, link, document_type, vendor, day, total, currency, rate = row
    return DocumentRecord(
        source_account=account,
        message_id=message_id,
        content_hash=content_hash,
        extraction=Extraction(
            document_type=document_type,  # pyright: ignore[reportArgumentType]
            vendor=vendor,
            invoice_date=date.fromisoformat(day),
            total=Decimal(total),
            currency=currency,
        ),
        file_link=link,
        inr_rate=Decimal(rate) if rate is not None else None,
    )


def _pending(row: tuple[Any, ...]) -> PendingDocument:
    record = _document(row[:10])
    return PendingDocument(
        content_hash=record.content_hash,
        extraction=record.extraction,
        file_link=record.file_link,
        doubts=tuple(Doubt(field, reason) for field, reason in json.loads(row[10])),
        inr_rate=record.inr_rate,
        read_again=bool(row[11]),
        source_account=record.source_account,
        message_id=record.message_id,
    )


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
        pending: tuple[PendingDocument, ...] = (),
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
                "DELETE FROM pending_documents WHERE source_account = ? AND message_id = ?", key
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
                "file_link, document_type, vendor, invoice_date, total, currency, inr_rate) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                        str(d.inr_rate) if d.inr_rate is not None else None,
                    )
                    for d in documents
                ],
            )
            self._db.executemany(
                "INSERT INTO pending_documents (source_account, message_id, content_hash, "
                "file_link, document_type, vendor, invoice_date, total, currency, inr_rate, "
                "doubts, read_again) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        *key,
                        p.content_hash,
                        p.file_link,
                        p.extraction.document_type,
                        p.extraction.vendor,
                        p.extraction.invoice_date.isoformat(),
                        str(p.extraction.total),
                        p.extraction.currency,
                        str(p.inr_rate) if p.inr_rate is not None else None,
                        json.dumps([[d.field, d.reason] for d in p.doubts]),
                        p.read_again,
                    )
                    for p in pending
                ],
            )
            if signal:
                self._db.execute(
                    "INSERT INTO billing_signals (source_account, message_id, kind, vendor) "
                    "VALUES (?, ?, ?, ?)",
                    (*key, signal.kind, signal.vendor),
                )

    def record_sync(
        self, month: CollectionMonth, source_account: str, reason: str | None = None
    ) -> None:
        """Records whether a source account could be read. A reason means it could not."""
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO syncs (collection_month, source_account, succeeded, "
                "reason) VALUES (?, ?, ?, ?)",
                (str(month), source_account, reason is None, reason),
            )

    def syncs(self, month: CollectionMonth) -> list[Sync]:
        rows = self._db.execute(
            "SELECT source_account, succeeded, reason FROM syncs "
            "WHERE collection_month = ? ORDER BY source_account",
            (str(month),),
        ).fetchall()
        return [Sync(account, bool(succeeded), reason) for account, succeeded, reason in rows]

    def save_expected_vendor(self, vendor: ExpectedVendor) -> None:
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO expected_vendors (vendor, source_account, "
                "billing_cycle, renewal_month, usual_amount, currency, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    vendor.vendor,
                    vendor.source_account,
                    vendor.billing_cycle,
                    vendor.renewal_month,
                    str(vendor.usual_amount) if vendor.usual_amount is not None else None,
                    vendor.currency,
                    vendor.status,
                ),
            )

    def remove_expected_vendor(self, vendor: str) -> None:
        with self._db:
            self._db.execute("DELETE FROM expected_vendors WHERE vendor = ?", (vendor,))

    def expected_vendors(self) -> list[ExpectedVendor]:
        """Every vendor on the list, whatever its status."""
        rows = self._db.execute(
            "SELECT vendor, source_account, billing_cycle, renewal_month, usual_amount, "
            "currency, status FROM expected_vendors ORDER BY vendor COLLATE NOCASE"
        ).fetchall()
        return [
            ExpectedVendor(
                vendor=vendor,
                source_account=account,
                billing_cycle=cycle,
                renewal_month=renewal_month,
                usual_amount=Decimal(usual) if usual is not None else None,
                currency=currency,
                status=status,
            )
            for vendor, account, cycle, renewal_month, usual, currency, status in rows
        ]

    def months(self) -> list[CollectionMonth]:
        """Every collection month that has been run, oldest first."""
        rows = self._db.execute(
            "SELECT collection_month FROM emails UNION SELECT collection_month FROM syncs "
            "ORDER BY 1"
        ).fetchall()
        return [CollectionMonth.parse(row[0]) for row in rows]

    def collected_in(self, email: Email) -> CollectionMonth | None:
        """The collection month this email was collected for, if it has been collected."""
        row = self._db.execute(
            "SELECT collection_month FROM emails "
            "WHERE source_account = ? AND message_id = ? AND state = ?",
            (email.source_account, email.message_id, EmailState.COLLECTED.value),
        ).fetchone()
        return CollectionMonth.parse(row[0]) if row else None

    def document_with(self, content_hash: str) -> DocumentRecord | None:
        """A billing document already collected with this content, from any source account."""
        row = self._db.execute(
            f"SELECT {_DOCUMENT_COLUMNS} FROM billing_documents d "
            "JOIN emails e USING (source_account, message_id) "
            "WHERE d.content_hash = ? AND e.state = ? "
            "ORDER BY e.received_at, d.source_account LIMIT 1",
            (content_hash, EmailState.COLLECTED.value),
        ).fetchone()
        return _document(row) if row else None

    def pending(self, month: CollectionMonth) -> list[PendingDocument]:
        """Billing documents of the month that are held for a person to confirm."""
        rows = self._db.execute(
            f"SELECT {_PENDING_COLUMNS} FROM pending_documents d "
            "JOIN emails e USING (source_account, message_id) "
            "WHERE e.collection_month = ? AND e.state = ? "
            "ORDER BY e.received_at, d.source_account, d.message_id, d.file_link",
            (str(month), EmailState.NEEDS_REVIEW.value),
        ).fetchall()
        return [_pending(row) for row in rows]

    def pending_with(self, content_hash: str) -> PendingDocument | None:
        """A billing document with this content that is already held."""
        row = self._db.execute(
            f"SELECT {_PENDING_COLUMNS} FROM pending_documents d "
            "JOIN emails e USING (source_account, message_id) "
            "WHERE d.content_hash = ? AND e.state = ? "
            "ORDER BY e.received_at, d.source_account LIMIT 1",
            (content_hash, EmailState.NEEDS_REVIEW.value),
        ).fetchone()
        return _pending(row) if row else None

    def documents(self, month: CollectionMonth) -> list[DocumentRecord]:
        rows = self._db.execute(
            f"SELECT {_DOCUMENT_COLUMNS} FROM billing_documents d "
            "JOIN emails e USING (source_account, message_id) "
            "WHERE e.collection_month = ? AND e.state = ? "
            "ORDER BY e.received_at, d.source_account, d.message_id, d.file_link",
            (str(month), EmailState.COLLECTED.value),
        ).fetchall()
        return [_document(row) for row in rows]

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
            "WHERE d.file_link = ? ORDER BY e.received_at, e.source_account LIMIT 1",
            (file_link,),
        ).fetchone()
        return SourceEmail(*row) if row else None

    def close(self) -> None:
        self._db.close()
