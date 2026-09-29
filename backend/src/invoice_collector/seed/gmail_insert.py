"""Puts generated messages into a source account through the Gmail API.

Messages are inserted, not sent, so each keeps its original sender and date.
Signing in is the caller's business: the service object arrives authorised for
the source account, with credentials that may write to it.
"""

import base64
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from invoice_collector.seed.generator import SeedMessage


@dataclass(frozen=True)
class InsertReport:
    """Message ids, in the order the messages were given."""

    inserted: tuple[str, ...]
    skipped: tuple[str, ...]


def _is_present(service: Any, message_id: str) -> bool:
    found = (
        service.users()
        .messages()
        .list(userId="me", q=f"rfc822msgid:{message_id}", includeSpamTrash=True, maxResults=1)
        .execute()
    )
    return bool(found.get("messages"))


def _as_sent(raw: bytes) -> bytes:
    """Mail travels with CRLF line ends; the sample files are kept with LF."""
    return raw.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")


def insert_messages(service: Any, messages: Iterable[SeedMessage]) -> InsertReport:
    """Inserts each message the source account does not hold yet.

    `service` is a Gmail API service object from `googleapiclient`. A message is
    looked up by its Message-ID first, so running this twice inserts nothing twice.
    """
    inserted: list[str] = []
    skipped: list[str] = []
    for message in messages:
        if _is_present(service, message.message_id):
            skipped.append(message.message_id)
            continue
        service.users().messages().insert(
            userId="me",
            internalDateSource="dateHeader",
            body={
                "raw": base64.urlsafe_b64encode(_as_sent(message.raw)).decode("ascii"),
                "labelIds": ["INBOX"],
            },
        ).execute()
        inserted.append(message.message_id)
    return InsertReport(tuple(inserted), tuple(skipped))
