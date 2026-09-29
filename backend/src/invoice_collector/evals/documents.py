"""The PDF the pipeline would produce for each golden billing document, made without a network.

An attachment is used as it is. An email body is rendered. A portal page is rendered from the
static copy in `samples/portal/`, and a page asking for a password is login-gated, so the
pipeline would hand it to a person instead of extracting it.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from invoice_collector.evals.golden import GoldenCase
from invoice_collector.evals.runner import content_id
from invoice_collector.extractor import pdf_text
from invoice_collector.renderer import Renderer
from invoice_collector.routing import Attachments, Body, NotBilling, PortalLink, route

_SIGN_IN_FIELD = re.compile(r"<input\b[^>]*\btype=[\"']?password", re.I)


@dataclass(frozen=True)
class Document:
    key: str
    """The golden case it was produced from."""
    pdf: bytes
    identity: str
    """A hash of what the PDF was made from. A rendered PDF differs from run to run in its
    metadata, so the cache is keyed by the source and not by the PDF's bytes."""
    text: str
    pages: int


@dataclass(frozen=True)
class NotProduced:
    key: str
    reason: str


def _document(key: str, pdf: bytes, identity: str) -> Document:
    text, pages = pdf_text(pdf)
    return Document(key, pdf, identity, text, pages)


def produce_documents(
    cases: Sequence[GoldenCase], renderer: Renderer, portal_pages: Path
) -> tuple[list[Document], list[NotProduced]]:
    documents: list[Document] = []
    not_produced: list[NotProduced] = []
    for case in cases:
        if not case.is_billing_document:
            continue
        routed = route(case.email)
        match routed:
            case Attachments(pdfs=pdfs):
                pdf = pdfs[0].content
                documents.append(_document(case.key, pdf, content_id("attachment", pdf)))
            case Body(html=html):
                pdf = renderer.render_html(html)
                documents.append(_document(case.key, pdf, content_id("body", html)))
            case PortalLink(url=url):
                page = portal_pages / Path(urlparse(url).path).name
                if not page.is_file():
                    not_produced.append(NotProduced(case.key, f"no static copy of {url}"))
                    continue
                html = page.read_text("utf-8")
                if _SIGN_IN_FIELD.search(html):
                    not_produced.append(
                        NotProduced(case.key, "login-gated portal link: needs an assisted download")
                    )
                    continue
                pdf = renderer.render_html(html)
                documents.append(_document(case.key, pdf, content_id("portal", html)))
            case NotBilling(reason=reason):
                not_produced.append(NotProduced(case.key, f"routing found nothing: {reason}"))
    return documents, not_produced
