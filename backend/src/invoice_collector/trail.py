"""What happened to each billing document, recorded at the moment it happened.

A run records how an email was classified, how its billing documents were found, which
model read each one and what it read, the expected vendor each was matched to and what
matched it, the checks that ran, and whether each was held or collected, filed and
converted to rupees. It also records each attempt at the email that had to be made
again, and a PDF that could not be opened and was held as it is. The dashboard records
filing on approval, the removal of the pending copy after a decision, and each upload
of a PDF downloaded by hand, with how it was read, checked and filed.
The ledger's other tables hold only the latest state; these events keep the history.

Each event has a kind, which is open: a kind this module does not name is kept and
shown like any other, so other parts of the tool can add their own. The details are
plain values. They hold facts about an email, never its body, and never a secret: a
portal link is kept as its host only, since a tokenised link opens the vendor's
billing page to whoever holds it.
"""

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from invoice_collector.checks import CheckResult
from invoice_collector.domain import Classification, Doubt, Extraction

SCHEMA = """
CREATE TABLE IF NOT EXISTS document_events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source_account TEXT NOT NULL,
    message_id     TEXT NOT NULL,
    content_hash   TEXT,
    kind           TEXT NOT NULL,
    happened_at    TEXT NOT NULL,
    actor          TEXT,
    details        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS document_events_by_hash ON document_events (content_hash);
CREATE INDEX IF NOT EXISTS document_events_by_email
    ON document_events (source_account, message_id);
"""

# The kinds a run and the Review screen record. Others may be added by other parts.
CLASSIFIED = "classified"
FOUND = "found"
READ = "read"
CHECKED = "checked"
READ_AGAIN = "read_again"
READ_AGAIN_FAILED = "read_again_failed"
HELD = "held"
COLLECTED = "collected"
LEFT_FOR_MONTH = "left_for_month"
FILED = "filed"
CONVERTED = "converted"
# The vendor as read was matched to an expected vendor, whose spelling it now carries.
MATCHED = "matched"
# An attempt at examining the email raised something unanticipated, and it was tried again.
RETRIED = "retried"
# The PDF could not be opened, damaged or password-protected, so it was held as it is.
UNOPENED = "unopened"
# After a review decision, the copy in the pending folder was removed, or could not be.
PENDING_COPY_REMOVED = "pending_copy_removed"
PENDING_COPY_NOT_REMOVED = "pending_copy_not_removed"
# A person uploaded the PDF they downloaded from a portal link that needs a sign-in.
UPLOADED = "uploaded"

# The actor of what the run did itself, rather than a model or a person.
RUN = "run"
# The actor of a match or a reading made by rules rather than a model.
RULES = "rules"


@dataclass(frozen=True)
class Event:
    """One thing that happened to an email or to a billing document found in it."""

    kind: str
    source_account: str
    message_id: str
    happened_at: datetime
    # None for what happened to the email as a whole, such as how it was classified.
    content_hash: str | None = None
    # Who or what did it: a model's name, "rules", a person's address, or "run".
    actor: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict[str, Any])


def _details(event: Event) -> str:
    return json.dumps(event.details, sort_keys=True, ensure_ascii=False)


def _key(event: Event) -> tuple[str, str, str | None, str]:
    return (event.source_account, event.message_id, event.content_hash, event.kind)


def append(db: sqlite3.Connection, events: Sequence[Event]) -> None:
    """Adds the events, in order, inside the caller's transaction.

    The events given of one kind for the same email and document are what one examination
    found of that kind, such as the checks of a reading, and of a second reading. They are
    not added again when they are the same, in order, as the latest as many of that kind
    already recorded: a run that examines an email again and finds the same does not make
    the history longer. When any of them differs, all of them are added, so a document
    read again with a different result shows the whole of the new reading.
    """
    given: dict[tuple[str, str, str | None, str], list[tuple[str | None, str]]] = {}
    for event in events:
        given.setdefault(_key(event), []).append((event.actor, _details(event)))
    recorded_already: set[tuple[str, str, str | None, str]] = set()
    for key, found in given.items():
        latest = db.execute(
            "SELECT actor, details FROM document_events WHERE source_account = ? "
            "AND message_id = ? AND content_hash IS ? AND kind = ? ORDER BY id DESC LIMIT ?",
            (*key, len(found)),
        ).fetchall()
        if [tuple(row) for row in reversed(latest)] == found:
            recorded_already.add(key)
    for event in events:
        if _key(event) in recorded_already:
            continue
        details = _details(event)
        db.execute(
            "INSERT INTO document_events (source_account, message_id, content_hash, kind, "
            "happened_at, actor, details) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                event.source_account,
                event.message_id,
                event.content_hash,
                event.kind,
                event.happened_at.isoformat(),
                event.actor,
                details,
            ),
        )


@dataclass(frozen=True)
class StoredEvent:
    """An event as read back, in the order it was recorded."""

    sequence: int
    event: Event


def events_of(db: sqlite3.Connection, content_hash: str) -> list[StoredEvent]:
    """Every event of the billing document, and of the emails it was found in.

    Events of an email as a whole, such as its classification, belong to every
    document found in it.
    """
    present = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'document_events'"
    ).fetchone()
    if present is None:
        return []  # A ledger from before events were recorded.
    rows = db.execute(
        "SELECT id, source_account, message_id, content_hash, kind, happened_at, actor, "
        "details FROM document_events WHERE content_hash = ? OR (content_hash IS NULL AND "
        "(source_account, message_id) IN (SELECT source_account, message_id "
        "FROM document_events WHERE content_hash = ?)) ORDER BY id",
        (content_hash, content_hash),
    ).fetchall()
    return [
        StoredEvent(
            sequence,
            Event(
                kind=kind,
                source_account=account,
                message_id=message_id,
                happened_at=datetime.fromisoformat(happened_at),
                content_hash=digest,
                actor=actor,
                details=json.loads(details),
            ),
        )
        for sequence, account, message_id, digest, kind, happened_at, actor, details in rows
    ]


# The details of the kinds recorded here, as plain values.


def fields_read(extraction: Extraction) -> dict[str, str]:
    """The fields read from a document, as they were read."""
    return {
        "vendor": extraction.vendor,
        "invoice_date": extraction.invoice_date.isoformat(),
        "total": str(extraction.total),
        "currency": extraction.currency,
        "document_type": extraction.document_type,
    }


def doubt_values(doubts: Sequence[Doubt]) -> list[dict[str, str | None]]:
    return [{"field": doubt.field, "reason": doubt.reason} for doubt in doubts]


def classified(classification: Classification) -> dict[str, Any]:
    return {
        "kind": classification.kind,
        "vendor": classification.vendor,
        "confidence": classification.confidence,
        "probability": classification.probability,
    }


def found(
    invoice_format: str, *, attachment: str | None = None, url: str | None = None
) -> dict[str, Any]:
    """How a document was found. Of a portal link, only its host is kept."""
    details: dict[str, Any] = {"invoice_format": invoice_format}
    if attachment is not None:
        details["attachment"] = attachment
    if url is not None:
        details["portal_host"] = urlsplit(url).hostname or ""
    return details


def read(extraction: Extraction) -> dict[str, Any]:
    return {
        "fields": fields_read(extraction),
        "confidence": extraction.confidence,
        "reader_note": extraction.doubts,
    }


def read_again(first: Extraction, second: Extraction) -> dict[str, Any]:
    """The second reading, and each field it read differently from the first."""
    before, after = fields_read(first), fields_read(second)
    return {
        **read(second),
        "changes": [
            {"field": name, "before": before[name], "after": after[name]}
            for name in before
            if before[name] != after[name]
        ],
    }


def checked(stage: str, results: Sequence[CheckResult]) -> dict[str, Any]:
    """The checks that ran at one stage, "reading" or "history", each with its result."""
    return {
        "stage": stage,
        "checks": [
            {
                "check": result.check,
                "passed": result.passed,
                "doubts": doubt_values(result.doubts),
            }
            for result in results
        ],
    }


def filed(link: str) -> dict[str, Any]:
    """Where a PDF was filed: the link the ledger keeps to it."""
    return {"file_link": link}


def converted(extraction: Extraction, rate: Any) -> dict[str, Any]:
    """The rate to rupees on the invoice date, or None when no rate was known."""
    return {
        "currency": extraction.currency,
        "rate": str(rate) if rate is not None else None,
        "rate_date": extraction.invoice_date.isoformat(),
    }


def matched(as_read: str, expected_vendor: str) -> dict[str, Any]:
    """The name the document gave, and the expected vendor it was matched to."""
    return {"as_read": as_read, "expected_vendor": expected_vendor}


def held(doubts: Sequence[Doubt], *, waits_with_email: bool = False) -> dict[str, Any]:
    """Held for a person: for its own doubts, or waiting with a doubted document of its email."""
    return {"doubts": doubt_values(doubts), "waits_with_email": waits_with_email}
