"""Turns a raw RFC 822 message into an email."""

from datetime import UTC, datetime
from email import message_from_bytes, policy
from email.message import EmailMessage, Message
from email.utils import parsedate_to_datetime

from invoice_collector.domain import Attachment, Email


def email_from_rfc822(
    source_account: str,
    raw: bytes,
    *,
    message_id: str | None = None,
    received_at: datetime | None = None,
) -> Email:
    """The email held in raw.

    The mailbox's own message id and received time are used when given; otherwise
    they are read from the Message-ID and Date headers.
    """
    message = message_from_bytes(raw, policy=policy.default)
    assert isinstance(message, EmailMessage)
    html = message.get_body(preferencelist=("html",))
    text = message.get_body(preferencelist=("plain",))
    return Email(
        source_account=source_account,
        message_id=message_id or str(message["Message-ID"] or "").strip().strip("<>"),
        sender=str(message["From"] or ""),
        subject=str(message["Subject"] or ""),
        received_at=received_at or _date_header(message),
        attachments=tuple(
            Attachment(
                filename=part.get_filename() or "",
                content_type=part.get_content_type(),
                content=_content(part),
            )
            for part in message.iter_attachments()
        ),
        html_body=_text(html) if html else None,
        text_body=_text(text) if text else None,
    )


def _date_header(message: EmailMessage) -> datetime:
    if message["Date"] is None:
        raise ValueError("the message has no Date header and no received time was given")
    received_at = parsedate_to_datetime(str(message["Date"]))
    return received_at if received_at.tzinfo else received_at.replace(tzinfo=UTC)


def _content(part: Message) -> bytes:
    payload = part.get_payload(decode=True)
    if isinstance(payload, bytes):
        return payload
    # An attached email has no payload of its own: keep it whole.
    children: object = part.get_payload()
    if not isinstance(children, list):
        return b""
    return b"".join(
        child.as_bytes()
        for child in children  # pyright: ignore[reportUnknownVariableType]
        if isinstance(child, Message)
    )


def _text(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if not isinstance(payload, bytes):
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")
