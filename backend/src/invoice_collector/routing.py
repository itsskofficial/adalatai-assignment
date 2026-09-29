"""Decides which invoice format a billing email has, by rules."""

import html
import re
from dataclasses import dataclass

from invoice_collector.domain import Attachment, Email, InvoiceFormat

_AMOUNT = re.compile(
    r"(?:[$€£₹]|\b(?:USD|EUR|GBP|INR|US\$)\s?)\s?\d[\d,]*\.\d{2}"
    r"|\d[\d,]*\.\d{2}\s?(?:USD|EUR|GBP|INR)\b"
)
_LINK = re.compile(r'<a\b[^>]*\bhref="(https?://[^"]+)"[^>]*>(.*?)</a>', re.I | re.S)
_BARE_URL = re.compile(r"https?://[^\s<>\"']+")
_LINK_WORDS = re.compile(r"invoice|receipt|billing|statement", re.I)


@dataclass(frozen=True)
class Attachments:
    pdfs: tuple[Attachment, ...]
    invoice_format = InvoiceFormat.ATTACHMENT


@dataclass(frozen=True)
class Body:
    html: str
    invoice_format = InvoiceFormat.BODY


@dataclass(frozen=True)
class PortalLink:
    url: str
    invoice_format = InvoiceFormat.PORTAL_LINK


@dataclass(frozen=True)
class NotBilling:
    reason: str


Route = Attachments | Body | PortalLink | NotBilling


def _is_pdf(attachment: Attachment) -> bool:
    return (
        attachment.content_type == "application/pdf"
        or attachment.filename.lower().endswith(".pdf")
        or attachment.content.startswith(b"%PDF-")
    )


def _as_html(email: Email) -> str:
    if email.html_body:
        return email.html_body
    return f"<pre>{html.escape(email.text_body or '')}</pre>"


def _portal_link(email: Email) -> str | None:
    links = [(url, text) for url, text in _LINK.findall(email.html_body or "")]
    links += [(url, "") for url in _BARE_URL.findall(email.text_body or "")]
    for url, text in links:
        if _LINK_WORDS.search(url) or _LINK_WORDS.search(text):
            return url
    # No link is followed on a guess: opening an unsubscribe link would act on it.
    return None


def route(email: Email) -> Route:
    pdfs = tuple(a for a in email.attachments if _is_pdf(a))
    if pdfs:
        return Attachments(pdfs)

    content = f"{email.html_body or ''}\n{email.text_body or ''}"
    if _AMOUNT.search(html.unescape(content)):
        return Body(_as_html(email))
    link = _portal_link(email)
    if link:
        return PortalLink(link)
    return NotBilling("no billing document found")
