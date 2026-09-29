"""Rendering and portal fetching with a headless browser."""

from types import TracebackType
from typing import Self

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright

from invoice_collector.portal import LoginGated, PortalFetchFailed

_SIGN_IN_FIELD = "input[type=password]"
_TIMEOUT_MS = 30_000


class HeadlessBrowser:
    """Implements both Renderer and PortalFetcher on one browser.

    It never types into a page or submits a form, so it cannot sign in anywhere.
    """

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

    def _page(self) -> Page:
        page = self._browser.new_page()
        page.set_default_timeout(_TIMEOUT_MS)
        return page

    def render_html(self, html: str) -> bytes:
        page = self._page()
        try:
            page.set_content(html, wait_until="load")
            return page.pdf(format="A4", print_background=True)
        finally:
            page.close()

    def fetch(self, url: str) -> bytes | LoginGated:
        page = self._page()
        try:
            direct = page.request.get(url)
            if direct.status in (401, 403):
                return LoginGated()
            if not direct.ok:
                raise PortalFetchFailed(f"could not open {url}: HTTP {direct.status}")
            if "application/pdf" in direct.headers.get("content-type", ""):
                return direct.body()

            page.goto(url, wait_until="networkidle")
            if page.locator(_SIGN_IN_FIELD).count() > 0:
                return LoginGated()
            return page.pdf(format="A4", print_background=True)
        except PlaywrightError as error:
            raise PortalFetchFailed(f"could not open {url}: {error.message}") from error
        finally:
            page.close()
