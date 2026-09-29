"""Turns an email body into a PDF."""

from typing import Protocol


class Renderer(Protocol):
    def render_html(self, html: str) -> bytes: ...


class FakeRenderer:
    """Produces a stand-in PDF whose content depends only on the input."""

    def render_html(self, html: str) -> bytes:
        return b"%PDF-rendered " + html.encode()
