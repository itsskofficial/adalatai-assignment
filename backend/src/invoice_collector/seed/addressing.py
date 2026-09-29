"""Readies a sample email for a real mailbox: its recipient, and where its portal links lead."""

from dataclasses import replace
from email import message_from_bytes, policy
from email.message import EmailMessage

from invoice_collector.seed.generator import SeedConfig, SeedMessage

# Where the generator points portal links unless told otherwise.
GENERATED_PORTAL_BASE_URL = SeedConfig.portal_base_url


def addressed_to(
    message: SeedMessage,
    address: str,
    portal_base_url: str | None = None,
    generated_portal_base_url: str = GENERATED_PORTAL_BASE_URL,
) -> SeedMessage:
    """The message as received at address, its portal links moved to portal_base_url.

    Every other header, the Message-ID among them, is kept, so a message already
    in the mailbox is still recognised.
    """
    parsed = message_from_bytes(message.raw, policy=policy.default)
    assert isinstance(parsed, EmailMessage)
    parsed.replace_header("To", address)
    if portal_base_url is not None:
        old, new = generated_portal_base_url.rstrip("/"), portal_base_url.rstrip("/")
        for part in parsed.walk():
            if part.get_content_maintype() != "text" or part.is_attachment():
                continue
            assert isinstance(part, EmailMessage)
            text = str(part.get_content())
            if old in text:
                part.set_content(
                    text.replace(old, new),
                    subtype=part.get_content_subtype(),
                    cte="quoted-printable",
                )
    return replace(message, raw=parsed.as_bytes())
