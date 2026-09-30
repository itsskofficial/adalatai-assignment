"""Who approved or rejected each held email, when, and what each field held before and after.

Kept in its own table in the ledger's SQLite file. The Ledger knows nothing of it; this
module opens the file itself for each read or write, as the dashboard's routes do.
"""

import json
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from invoice_collector.database import connect
from invoice_collector.domain import CollectionMonth, Extraction

ReviewAction = Literal["approved", "rejected"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS review_decisions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    collection_month TEXT NOT NULL,
    source_account   TEXT NOT NULL,
    message_id       TEXT NOT NULL,
    subject          TEXT NOT NULL,
    action           TEXT NOT NULL,
    person           TEXT NOT NULL,
    decided_at       TEXT NOT NULL,
    documents        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS review_decisions_by_month ON review_decisions (collection_month);
"""


def amount(total: Decimal) -> str:
    """Two decimals, unless that would round away digits a person typed."""
    shown = f"{total:.2f}"
    return shown if Decimal(shown) == total else str(total)


def fields_of(extraction: Extraction) -> dict[str, str]:
    """The fields a person confirms, as plain values."""
    return {
        "vendor": extraction.vendor,
        "invoice_date": extraction.invoice_date.isoformat(),
        "total": amount(extraction.total),
        "currency": extraction.currency,
        "document_type": extraction.document_type,
    }


def changed_fields(before: Extraction, after: Extraction) -> list[str]:
    """The fields a person changed. A total written differently but equal is unchanged."""
    changed: list[str] = []
    if before.vendor != after.vendor:
        changed.append("vendor")
    if before.invoice_date != after.invoice_date:
        changed.append("invoice_date")
    if before.total != after.total:
        changed.append("total")
    if before.currency != after.currency:
        changed.append("currency")
    if before.document_type != after.document_type:
        changed.append("document_type")
    return changed


@dataclass(frozen=True)
class DocumentDecision:
    """One billing document of a decided email: as it was read, and as it was confirmed."""

    content_hash: str
    before: Extraction
    # None when the email was judged not to be a billing document.
    after: Extraction | None


@dataclass(frozen=True)
class ReviewDecisionRecord:
    collection_month: str
    source_account: str
    message_id: str
    subject: str
    action: ReviewAction
    person: str
    decided_at: datetime
    documents: list[dict[str, Any]]


def _document_values(document: DocumentDecision) -> dict[str, Any]:
    return {
        "content_hash": document.content_hash,
        "before": fields_of(document.before),
        "after": fields_of(document.after) if document.after is not None else None,
        "changed_fields": changed_fields(document.before, document.after)
        if document.after is not None
        else [],
    }


class ReviewHistory:
    def __init__(self, ledger_path: Path) -> None:
        self._path = ledger_path

    def _connect(self) -> sqlite3.Connection:
        db = connect(self._path)
        db.executescript(SCHEMA)
        return db

    def record(
        self,
        month: CollectionMonth,
        *,
        source_account: str,
        message_id: str,
        subject: str,
        action: ReviewAction,
        person: str,
        decided_at: datetime,
        documents: Sequence[DocumentDecision],
    ) -> ReviewDecisionRecord:
        values = [_document_values(d) for d in documents]
        with closing(self._connect()) as db, db:
            db.execute(
                "INSERT INTO review_decisions (collection_month, source_account, message_id, "
                "subject, action, person, decided_at, documents) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(month),
                    source_account,
                    message_id,
                    subject,
                    action,
                    person,
                    decided_at.isoformat(),
                    json.dumps(values),
                ),
            )
        return ReviewDecisionRecord(
            collection_month=str(month),
            source_account=source_account,
            message_id=message_id,
            subject=subject,
            action=action,
            person=person,
            decided_at=decided_at,
            documents=values,
        )

    def of(self, month: CollectionMonth) -> list[ReviewDecisionRecord]:
        """Every decision on the month's held emails, newest first."""
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT collection_month, source_account, message_id, subject, action, person, "
                "decided_at, documents FROM review_decisions WHERE collection_month = ? "
                "ORDER BY decided_at DESC, id DESC",
                (str(month),),
            ).fetchall()
        return [
            ReviewDecisionRecord(
                collection_month=collection_month,
                source_account=account,
                message_id=message_id,
                subject=subject,
                action=action,
                person=person,
                decided_at=datetime.fromisoformat(decided_at),
                documents=json.loads(documents),
            )
            for (
                collection_month,
                account,
                message_id,
                subject,
                action,
                person,
                decided_at,
                documents,
            ) in rows
        ]
