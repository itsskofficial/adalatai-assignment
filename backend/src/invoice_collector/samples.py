"""Loads source accounts and prepared answers from a folder of sample emails.

Layout: one folder per source account holding .eml files, and an answers.json
mapping the SHA-256 of each PDF to its extraction. The portal folder holds the pages the
sample portal serves, and is not a source account.
"""

import json
from datetime import UTC, date
from decimal import Decimal
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from pathlib import Path

from invoice_collector.domain import Attachment, Email, Extraction
from invoice_collector.extractor import FakeExtractor
from invoice_collector.mail_source import InMemoryMailSource

PORTAL_FOLDER = "portal"


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
        if folder.is_dir() and folder.name != PORTAL_FOLDER
    ]


def load_extractor(root: Path) -> FakeExtractor:
    answers = json.loads((root / "answers.json").read_text(encoding="utf-8"))
    return FakeExtractor(
        {
            digest: Extraction(
                document_type=a["document_type"],
                vendor=a["vendor"],
                invoice_date=date.fromisoformat(a["invoice_date"]),
                total=Decimal(a["total"]),
                currency=a["currency"],
            )
            for digest, a in answers.items()
        }
    )
