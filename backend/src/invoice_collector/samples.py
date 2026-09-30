"""Loads source accounts from a folder of sample emails.

Layout: one folder per source account holding .eml files. The portal folder holds the pages
the sample portal serves, and is not a source account. Nor is a folder named for a collection
month, YYYY-MM: it holds a set of that month alone, in the same layout. The answer key
generated beside the emails, answers.json, is for the eval and the tests; a run never reads it.
"""

from datetime import UTC
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from pathlib import Path

from invoice_collector.domain import Attachment, CollectionMonth, Email
from invoice_collector.mail_source import InMemoryMailSource

PORTAL_FOLDER = "portal"


def is_month_folder(name: str) -> bool:
    """Whether a folder of the samples is named for a collection month, as 2026-09 is."""
    try:
        return str(CollectionMonth.parse(name)) == name
    except ValueError:
        return False


def _email_from_eml(source_account: str, path: Path) -> Email:
    message = message_from_bytes(path.read_bytes(), policy=policy.default)
    assert isinstance(message, EmailMessage)
    if message["Date"] is None:
        raise ValueError(f"{path} has no Date header")
    received_at = parsedate_to_datetime(str(message["Date"]))
    if received_at.tzinfo is None:
        received_at = received_at.replace(tzinfo=UTC)
    attachments = tuple(
        Attachment(
            filename=part.get_filename() or "",
            content_type=part.get_content_type(),
            content=part.get_payload(decode=True),  # pyright: ignore[reportArgumentType]
        )
        for part in message.iter_attachments()
    )
    html = message.get_body(preferencelist=("html",))
    text = message.get_body(preferencelist=("plain",))
    return Email(
        html_body=str(html.get_content()) if html else None,
        text_body=str(text.get_content()) if text else None,
        source_account=source_account,
        message_id=str(message["Message-ID"] or path.stem).strip("<>"),
        sender=str(message["From"]),
        subject=str(message["Subject"]),
        received_at=received_at,
        attachments=attachments,
    )


def load_sources(root: Path) -> list[InMemoryMailSource]:
    return [
        InMemoryMailSource(
            folder.name, [_email_from_eml(folder.name, p) for p in sorted(folder.glob("*.eml"))]
        )
        for folder in sorted(root.iterdir())
        if folder.is_dir() and folder.name != PORTAL_FOLDER and not is_month_folder(folder.name)
    ]
