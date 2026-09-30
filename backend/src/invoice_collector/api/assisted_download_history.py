"""Who uploaded each assisted download, when, and what became of it.

Kept in its own table in the ledger's SQLite file, as the review history is. The Ledger
knows nothing of it; this module opens the file itself for each read or write.
"""

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from invoice_collector.database import connect

# collected: filed and reported. held: held for review, since a check raised a doubt.
# already_collected: the same billing document had been collected before, so the email
# was linked to it and nothing new was filed.
UploadOutcome = Literal["collected", "held", "already_collected"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS assisted_downloads (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    collection_month TEXT NOT NULL,
    source_account   TEXT NOT NULL,
    message_id       TEXT NOT NULL,
    subject          TEXT NOT NULL,
    portal_link      TEXT,
    file_hash        TEXT NOT NULL,
    size             INTEGER NOT NULL,
    content_hash     TEXT NOT NULL,
    file_name        TEXT NOT NULL,
    outcome          TEXT NOT NULL,
    document         TEXT NOT NULL,
    person           TEXT NOT NULL,
    uploaded_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS assisted_downloads_by_email
    ON assisted_downloads (source_account, message_id);
CREATE INDEX IF NOT EXISTS assisted_downloads_by_file ON assisted_downloads (file_hash);
"""

_COLUMNS = (
    "collection_month, source_account, message_id, subject, portal_link, file_hash, size, "
    "content_hash, file_name, outcome, document, person, uploaded_at"
)


@dataclass(frozen=True)
class UploadRecord:
    # The collection month the billing document was filed or held under.
    collection_month: str
    source_account: str
    message_id: str
    subject: str
    portal_link: str | None
    # The SHA-256 of the uploaded bytes, which recognises the same file uploaded again.
    file_hash: str
    size: int
    # The identity the billing document is recorded under in the ledger.
    content_hash: str
    file_name: str
    outcome: UploadOutcome
    # The fields as read or as already collected, and the doubts raised: {"fields": {...},
    # "doubts": [{"field": ..., "reason": ...}]}.
    document: dict[str, Any]
    person: str
    uploaded_at: datetime


def _outcome(text: str) -> UploadOutcome:
    if text == "held":
        return "held"
    if text == "already_collected":
        return "already_collected"
    return "collected"


def _record(row: tuple[Any, ...]) -> UploadRecord:
    (
        month,
        account,
        message_id,
        subject,
        link,
        file_hash,
        size,
        digest,
        name,
        outcome,
        document,
        person,
        at,
    ) = row
    return UploadRecord(
        collection_month=month,
        source_account=account,
        message_id=message_id,
        subject=subject,
        portal_link=link,
        file_hash=file_hash,
        size=int(size),
        content_hash=digest,
        file_name=name,
        outcome=_outcome(outcome),
        document=json.loads(document),
        person=person,
        uploaded_at=datetime.fromisoformat(at),
    )


class AssistedDownloadHistory:
    def __init__(self, ledger_path: Path) -> None:
        self._path = ledger_path

    def _connect(self) -> sqlite3.Connection:
        db = connect(self._path)
        db.executescript(SCHEMA)
        return db

    def record(self, upload: UploadRecord) -> None:
        with closing(self._connect()) as db, db:
            db.execute(
                f"INSERT INTO assisted_downloads ({_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    upload.collection_month,
                    upload.source_account,
                    upload.message_id,
                    upload.subject,
                    upload.portal_link,
                    upload.file_hash,
                    upload.size,
                    upload.content_hash,
                    upload.file_name,
                    upload.outcome,
                    json.dumps(upload.document),
                    upload.person,
                    upload.uploaded_at.isoformat(),
                ),
            )

    def of_email(self, source_account: str, message_id: str) -> list[UploadRecord]:
        """Every upload for one email, newest first."""
        with closing(self._connect()) as db:
            rows = db.execute(
                f"SELECT {_COLUMNS} FROM assisted_downloads "
                "WHERE source_account = ? AND message_id = ? ORDER BY uploaded_at DESC, id DESC",
                (source_account, message_id),
            ).fetchall()
        return [_record(row) for row in rows]

    def of_file(self, file_hash: str) -> list[UploadRecord]:
        """Every upload of the same bytes, for any email, oldest first."""
        with closing(self._connect()) as db:
            rows = db.execute(
                f"SELECT {_COLUMNS} FROM assisted_downloads WHERE file_hash = ? "
                "ORDER BY uploaded_at, id",
                (file_hash,),
            ).fetchall()
        return [_record(row) for row in rows]

    def of_month(self, month: str) -> list[UploadRecord]:
        """Every upload filed or held under the collection month, newest first."""
        with closing(self._connect()) as db:
            rows = db.execute(
                f"SELECT {_COLUMNS} FROM assisted_downloads WHERE collection_month = ? "
                "ORDER BY uploaded_at DESC, id DESC",
                (month,),
            ).fetchall()
        return [_record(row) for row in rows]
