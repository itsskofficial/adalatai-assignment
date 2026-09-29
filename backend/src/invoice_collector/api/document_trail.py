"""The history of one billing document, in order of time, as the dashboard shows it.

Brought together from what the ledger records: each email the document was found in,
the events a run and the Review screen recorded as they happened (see trail.py), and
each review decision with the fields a person corrected. A document collected before
events were recorded has a shorter history, filled in from its latest state where that
is known.

Only facts about an email are shown: its sender, subject, when it arrived and where.
Never its body, and never a portal link in full, which may open the vendor's billing
page to whoever holds it.
"""

import json
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi import Path as PathParameter
from pydantic import BaseModel

from invoice_collector import trail
from invoice_collector.api.month_summary import file_name, file_url
from invoice_collector.domain import CollectionMonth, EmailState

# A content hash is the SHA-256 of what the document was made from. See ADR 0013.
CONTENT_HASH_PATTERN = r"^[0-9a-f]{64}$"

RECEIVED = "received"
CORRECTED = "corrected"
APPROVED = "approved"
REJECTED = "rejected"


class TrailEmail(BaseModel):
    source_account: str
    message_id: str
    sender: str
    subject: str
    received_at: str
    collection_month: str
    state: EmailState
    reason: str | None


class TrailEntry(BaseModel):
    """One step in the history. The kind is open: a kind the reader does not know is shown
    by its name and details."""

    kind: str
    # None for a step recorded before events were, whose time is not known.
    at: str | None
    source_account: str | None
    # Who or what did it: a model's name, "rules", a person's address, or "run".
    actor: str | None
    details: dict[str, Any]


class DocumentTrail(BaseModel):
    content_hash: str
    # The fields as they stand now: as confirmed, collected, held, or last read.
    fields: dict[str, str] | None
    # collected, needs_review, rejected, or not_collected.
    state: str
    collection_month: str | None
    invoice_format: str | None
    source_accounts: list[str]
    file_name: str | None
    file_url: str | None
    emails: list[TrailEmail]
    entries: list[TrailEntry]
    # True when the document was read before events were recorded, so its history is short.
    recorded_before_trail: bool


@dataclass(frozen=True)
class _Holding:
    """A row of billing_documents or pending_documents for this document."""

    source_account: str
    message_id: str
    file_link: str
    fields: dict[str, str]
    inr_rate: str | None
    doubts: list[dict[str, str | None]] | None
    read_again: bool


@dataclass(frozen=True)
class _Sortable:
    at: datetime | None
    group: int
    sequence: int
    entry: TrailEntry


def _moment(text: str) -> datetime:
    moment = datetime.fromisoformat(text)
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _has_table(db: sqlite3.Connection, name: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _holdings(db: sqlite3.Connection, table: str, content_hash: str) -> list[_Holding]:
    pending = table == "pending_documents"
    extra = ", doubts, read_again" if pending else ", NULL, 0"
    rows = db.execute(
        "SELECT source_account, message_id, file_link, document_type, vendor, invoice_date, "
        f"total, currency, inr_rate{extra} FROM {table} WHERE content_hash = ? "
        "ORDER BY source_account, message_id",
        (content_hash,),
    ).fetchall()
    return [
        _Holding(
            source_account=account,
            message_id=message_id,
            file_link=link,
            fields={
                "vendor": vendor,
                "invoice_date": day,
                "total": total,
                "currency": currency,
                "document_type": document_type,
            },
            inr_rate=rate,
            doubts=[{"field": f, "reason": r} for f, r in json.loads(doubts)] if doubts else None,
            read_again=bool(read_again),
        )
        for (
            account,
            message_id,
            link,
            document_type,
            vendor,
            day,
            total,
            currency,
            rate,
            doubts,
            read_again,
        ) in rows
    ]


def _decisions(db: sqlite3.Connection, content_hash: str) -> list[tuple[Any, ...]]:
    if not _has_table(db, "review_decisions"):
        return []
    rows = db.execute(
        "SELECT id, source_account, message_id, action, person, decided_at, documents "
        "FROM review_decisions WHERE documents LIKE ? ORDER BY id",
        (f"%{content_hash}%",),
    ).fetchall()
    found: list[tuple[Any, ...]] = []
    for sequence, account, message_id, action, person, decided_at, documents in rows:
        for document in json.loads(documents):
            if document.get("content_hash") == content_hash:
                found.append((sequence, account, message_id, action, person, decided_at, document))
    return found


def _emails(db: sqlite3.Connection, keys: Sequence[tuple[str, str]]) -> list[TrailEmail]:
    emails: list[TrailEmail] = []
    for account, message_id in keys:
        row = db.execute(
            "SELECT sender, subject, received_at, collection_month, state, reason FROM emails "
            "WHERE source_account = ? AND message_id = ?",
            (account, message_id),
        ).fetchone()
        if row is None:
            continue
        sender, subject, received_at, month, state, reason = row
        emails.append(
            TrailEmail(
                source_account=account,
                message_id=message_id,
                sender=sender,
                subject=subject,
                received_at=received_at,
                collection_month=month,
                state=EmailState(state),
                reason=reason,
            )
        )
    return sorted(emails, key=lambda e: (_moment(e.received_at), e.source_account))


def _is_web_link(link: str) -> bool:
    return link.startswith(("https://", "http://"))


def _url_of(month: str, link: str, held: bool) -> str:
    """Where the dashboard opens the document's PDF: as the Summary or the Review screen does."""
    if _is_web_link(link) or not held:
        return file_url(CollectionMonth.parse(month), link)
    return f"/api/months/{month}/review/billing-documents/{quote(file_name(link), safe='')}"


def _shown(event: trail.Event, current_link: str | None) -> dict[str, Any]:
    """The details of an event as the dashboard shows them."""
    details = dict(event.details)
    if event.kind == trail.FILED and isinstance(details.get("file_link"), str):
        # Where a file is kept on this machine is not shown; its name is.
        link: str = details.pop("file_link")
        details["file_name"] = file_name(link)
        details["pending"] = "/pending/" in link.replace("\\", "/")
        details["current"] = link == current_link
        details["web_link"] = link if _is_web_link(link) else None
    return details


def document_trail(ledger_path: Path, content_hash: str) -> DocumentTrail | None:
    """The history of the billing document, or None if the ledger knows nothing of it."""
    if not ledger_path.is_file():
        return None
    uri = f"{ledger_path.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        collected = _holdings(db, "billing_documents", content_hash)
        pending = _holdings(db, "pending_documents", content_hash)
        stored = trail.events_of(db, content_hash)
        decisions = _decisions(db, content_hash)
        keys: dict[tuple[str, str], None] = {}
        for holding in (*collected, *pending):
            keys[(holding.source_account, holding.message_id)] = None
        for each in stored:
            if each.event.content_hash == content_hash:
                keys[(each.event.source_account, each.event.message_id)] = None
        for decision in decisions:
            keys[(decision[1], decision[2])] = None
        emails = _emails(db, list(keys))
        formats = [
            row[0]
            for account, message_id in keys
            if (
                row := db.execute(
                    "SELECT invoice_format FROM emails WHERE source_account = ? AND message_id = ?",
                    (account, message_id),
                ).fetchone()
            )
            is not None
            and row[0] is not None
        ]
    if not emails and not stored:
        return None
    return _assemble(content_hash, emails, formats, collected, pending, stored, decisions)


def _assemble(
    content_hash: str,
    emails: list[TrailEmail],
    formats: list[str],
    collected: list[_Holding],
    pending: list[_Holding],
    stored: list[trail.StoredEvent],
    decisions: list[tuple[Any, ...]],
) -> DocumentTrail:
    by_email = {(e.source_account, e.message_id): e for e in emails}
    holding = next(
        (h for h in collected if by_email.get((h.source_account, h.message_id)) is not None),
        None,
    ) or next(iter(pending), None)
    held = holding is not None and not collected
    month = (
        by_email[(holding.source_account, holding.message_id)].collection_month
        if holding is not None and (holding.source_account, holding.message_id) in by_email
        else (emails[0].collection_month if emails else None)
    )
    current_link = holding.file_link if holding is not None else None

    entries: list[_Sortable] = []
    for email in emails:
        entries.append(
            _Sortable(
                _moment(email.received_at),
                0,
                0,
                TrailEntry(
                    kind=RECEIVED,
                    at=email.received_at,
                    source_account=email.source_account,
                    actor=None,
                    details={
                        "sender": email.sender,
                        "subject": email.subject,
                        "message_id": email.message_id,
                    },
                ),
            )
        )
    for each in stored:
        event = each.event
        entries.append(
            _Sortable(
                _moment(event.happened_at.isoformat()),
                2,
                each.sequence,
                TrailEntry(
                    kind=event.kind,
                    at=event.happened_at.isoformat(),
                    source_account=event.source_account,
                    actor=event.actor,
                    details=_shown(event, current_link),
                ),
            )
        )
    for sequence, account, _message, action, person, decided_at, document in decisions:
        at = _moment(decided_at)
        before: dict[str, str] = document.get("before") or {}
        after: dict[str, str] = document.get("after") or {}
        changed: list[str] = list(document.get("changed_fields") or [])
        for name in changed:
            entries.append(
                _Sortable(
                    at,
                    1,
                    sequence,
                    TrailEntry(
                        kind=CORRECTED,
                        at=decided_at,
                        source_account=account,
                        actor=person,
                        details={
                            "field": name,
                            "before": before.get(name),
                            "after": after.get(name),
                        },
                    ),
                )
            )
        entries.append(
            _Sortable(
                at,
                1,
                sequence,
                TrailEntry(
                    kind=APPROVED if action == "approved" else REJECTED,
                    at=decided_at,
                    source_account=account,
                    actor=person,
                    details={"changed_fields": changed},
                ),
            )
        )

    kinds = {each.event.kind for each in stored if each.event.content_hash == content_hash}
    entries.extend(_from_latest_state(kinds, holding, held))

    last_received = max((_moment(e.received_at) for e in emails), default=None)
    ordered = sorted(
        entries,
        key=lambda s: (
            s.at or last_received or datetime.min.replace(tzinfo=UTC),
            s.group,
            s.sequence,
        ),
    )
    fields = holding.fields if holding is not None else _last_read(stored, content_hash)
    rejected = any(d[3] == "rejected" for d in decisions) and holding is None
    state = (
        "collected"
        if collected
        else "needs_review"
        if pending
        else "rejected"
        if rejected
        else "not_collected"
    )
    accounts = sorted({e.source_account for e in emails})
    return DocumentTrail(
        content_hash=content_hash,
        fields=fields,
        state=state,
        collection_month=month,
        invoice_format=formats[0] if formats else None,
        source_accounts=accounts,
        file_name=file_name(current_link) if current_link is not None else None,
        file_url=_url_of(month, current_link, held)
        if current_link is not None and month is not None
        else None,
        emails=emails,
        entries=[s.entry for s in ordered],
        recorded_before_trail=trail.READ not in kinds,
    )


def _from_latest_state(kinds: set[str], holding: _Holding | None, held: bool) -> list[_Sortable]:
    """Steps recorded before events were, filled in from what the ledger holds now.

    Their time is not known, so they follow the arrival of the email.
    """
    if holding is None:
        return []
    account = holding.source_account
    steps: list[TrailEntry] = []
    if held and trail.HELD not in kinds:
        steps.append(
            TrailEntry(
                kind=trail.HELD,
                at=None,
                source_account=account,
                actor=None,
                details={"doubts": holding.doubts or [], "read_again": holding.read_again},
            )
        )
    if trail.FILED not in kinds:
        link = holding.file_link
        steps.append(
            TrailEntry(
                kind=trail.FILED,
                at=None,
                source_account=account,
                actor=None,
                details={
                    "file_name": file_name(link),
                    "pending": held,
                    "current": True,
                    "web_link": link if _is_web_link(link) else None,
                },
            )
        )
    if not held and trail.CONVERTED not in kinds:
        steps.append(
            TrailEntry(
                kind=trail.CONVERTED,
                at=None,
                source_account=account,
                actor=None,
                details={
                    "currency": holding.fields["currency"],
                    "rate": holding.inr_rate,
                    "rate_date": holding.fields["invoice_date"],
                },
            )
        )
    return [_Sortable(None, 1, 0, step) for step in steps]


def _last_read(stored: list[trail.StoredEvent], content_hash: str) -> dict[str, str] | None:
    for each in reversed(stored):
        event = each.event
        if event.content_hash == content_hash and event.kind in (trail.READ, trail.READ_AGAIN):
            fields = event.details.get("fields")
            if isinstance(fields, dict):
                return {str(k): str(v) for k, v in fields.items()}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    return None


def trail_routes(ledger_path: Path) -> APIRouter:
    """The route that gives the history of one billing document, by its content hash."""
    router = APIRouter()

    @router.get("/billing-documents/{content_hash}/trail")
    def history(  # pyright: ignore[reportUnusedFunction]
        content_hash: Annotated[str, PathParameter(pattern=CONTENT_HASH_PATTERN)],
    ) -> DocumentTrail:
        found = document_trail(ledger_path, content_hash)
        if found is None:
            raise HTTPException(status_code=404, detail="No such billing document")
        return found

    return router
