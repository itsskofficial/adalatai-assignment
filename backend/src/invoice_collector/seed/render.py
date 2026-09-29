"""Renders the HTML of a billing document to a PDF with a headless browser."""

from types import TracebackType
from typing import Self

from playwright.sync_api import sync_playwright


class BrowserRenderer:
    def __enter__(self) -> Self:
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._browser.close()
        self._playwright.stop()

    def render_html(self, html: str) -> bytes:
        page = self._browser.new_page()
        try:
            page.set_content(html, wait_until="load")
            return page.pdf(format="A4")
        finally:
            page.close()
