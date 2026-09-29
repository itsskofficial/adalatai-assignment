"""Rendering and portal fetching with a headless browser.

Both the body of an email and the page behind a link are chosen by whoever sent the
email, so neither is trusted:

- An email body is rendered with scripts off and with no network access at all.
- A portal link is opened only if the destination policy allows it, and the same policy
  is applied to every redirect and to everything the page goes on to request.
- The browser never types into a page or submits a form, so it cannot sign in anywhere.
"""

from types import TracebackType
from typing import Self
from urllib.parse import urljoin

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, Route, sync_playwright

from invoice_collector.destinations import DestinationPolicy
from invoice_collector.portal import LoginGated, PortalFetchFailed

_SIGN_IN_FIELD = "input[type=password]"
_TIMEOUT_MS = 30_000
_MAX_REDIRECTS = 5
_REDIRECTS = (301, 302, 303, 307, 308)


class HeadlessBrowser:
    """Implements both Renderer and PortalFetcher on one browser."""

    def __init__(self, policy: DestinationPolicy | None = None) -> None:
        self._policy = policy or DestinationPolicy()

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

    def _page(self, *, scripts: bool) -> Page:
        page = self._browser.new_page(java_script_enabled=scripts, service_workers="block")
        page.set_default_timeout(_TIMEOUT_MS)
        return page

    def render_html(self, html: str) -> bytes:
        page = self._page(scripts=False)
        try:
            page.route("**/*", _only_inline_data)
            page.set_content(html, wait_until="load")
            return page.pdf(format="A4", print_background=True)
        finally:
            page.close()

    def fetch(self, url: str) -> bytes | LoginGated:
        page = self._page(scripts=True)
        try:
            page.route("**/*", self._within_policy)
            return self._fetch(page, url)
        except PlaywrightError as error:
            raise PortalFetchFailed(f"could not open {url}: {error.message}") from error
        finally:
            page.close()

    def _fetch(self, page: Page, url: str) -> bytes | LoginGated:
        # Redirects are followed one at a time, so each destination can be checked.
        for _ in range(_MAX_REDIRECTS + 1):
            refusal = self._policy.refusal(url)
            if refusal:
                raise PortalFetchFailed(f"could not open {url}: {refusal}")

            response = page.request.get(url, max_redirects=0)
            if response.status in _REDIRECTS and "location" in response.headers:
                url = urljoin(url, response.headers["location"])
                continue
            if response.status in (401, 403):
                return LoginGated()
            if not response.ok:
                raise PortalFetchFailed(f"could not open {url}: HTTP {response.status}")
            if "application/pdf" in response.headers.get("content-type", ""):
                return response.body()

            page.goto(url, wait_until="networkidle")
            if page.locator(_SIGN_IN_FIELD).count() > 0:
                return LoginGated()
            return page.pdf(format="A4", print_background=True)
        raise PortalFetchFailed(f"could not open {url}: too many redirects")

    def _within_policy(self, route: Route) -> None:
        """Applied to everything the page requests, including what it is redirected to."""
        if self._policy.refusal(route.request.url):
            route.abort("blockedbyclient")
            return
        response = route.fetch(max_redirects=0)
        if response.status in _REDIRECTS:
            route.abort("blockedbyclient")
            return
        route.fulfill(response=response)


def _only_inline_data(route: Route) -> None:
    if route.request.url.startswith("data:"):
        route.continue_()
    else:
        route.abort("blockedbyclient")
