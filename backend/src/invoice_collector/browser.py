"""Rendering and portal fetching with a headless browser.

Both the body of an email and the page behind a link are chosen by whoever sent the
email, so neither is trusted:

- An email body is rendered with scripts off and with no network access at all.
- A portal link is opened only if the destination policy allows it, and the same policy
  is applied to every redirect and to everything the page goes on to request.
- Every connection the browser makes passes through a proxy that checks the address
  again at the moment of connecting, so a name that changes its answer is caught.
- The browser never types into a page or submits a form, so it cannot sign in anywhere.

A portal page is requested once. What is rendered is the response that was checked,
because a link carrying a single-use token may not answer a second time.
"""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, suppress
from dataclasses import dataclass
from types import TracebackType
from typing import Protocol, Self
from urllib.parse import urljoin

from playwright.sync_api import APIResponse, Page, Route, sync_playwright
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from invoice_collector.destinations import DestinationPolicy
from invoice_collector.pinning_proxy import PinningProxy
from invoice_collector.portal import LoginGated, PortalFetcher, PortalFetchFailed
from invoice_collector.renderer import Renderer, RenderFailed

_SIGN_IN_FIELD = "input[type=password]"
_TIMEOUT_MS = 30_000
# A page that polls, or holds a request open, never goes quiet. It is given this long
# to settle, and is then collected as it stands.
_SETTLE_MS = 5_000
_MAX_REDIRECTS = 5
_REDIRECTS = (301, 302, 303, 307, 308)
_BLANK = "<html><body></body></html>"


@dataclass
class _Visit:
    """What came back when the page itself was requested."""

    status: int | None = None
    redirect: str | None = None
    pdf: bytes | None = None

    def clear(self) -> None:
        self.status, self.redirect, self.pdf = None, None, None


def _redirect_target(requested: str, response: APIResponse) -> str | None:
    if response.status in _REDIRECTS and "location" in response.headers:
        return urljoin(requested, response.headers["location"])
    return None


class HeadlessBrowser:
    """Implements both Renderer and PortalFetcher on one browser."""

    def __init__(
        self,
        policy: DestinationPolicy | None = None,
        settle_ms: int = _SETTLE_MS,
        proxy: PinningProxy | None = None,
    ) -> None:
        self._policy = policy or DestinationPolicy()
        self._settle_ms = settle_ms
        # Unless addresses inside the network are allowed on purpose, every connection
        # goes through a proxy that checks the address at the moment it connects.
        self._proxy = proxy
        if proxy is None and not self._policy.allow_private_addresses:
            self._proxy = PinningProxy()

    def __enter__(self) -> Self:
        self._playwright = sync_playwright().start()
        if self._proxy is None:
            self._browser = self._playwright.chromium.launch()
        else:
            self._proxy.__enter__()
            # "<-loopback>" stops the browser from going straight to this machine. The sample
            # portal, when one is allowed, is reached directly: the proxy makes only secure
            # connections to public addresses. Every request is still checked against the
            # policy first, which allows that one address and no other.
            bypass = ",".join(["<-loopback>", *filter(None, [self._policy.sample_portal_host])])
            self._browser = self._playwright.chromium.launch(
                proxy={"server": self._proxy.address, "bypass": bypass}
            )
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._browser.close()
        self._playwright.stop()
        if self._proxy is not None:
            self._proxy.__exit__(exc_type, exc, traceback)

    def _page(self, *, scripts: bool) -> Page:
        page = self._browser.new_page(java_script_enabled=scripts, service_workers="block")
        page.set_default_timeout(_TIMEOUT_MS)
        return page

    def render_html(self, html: str) -> bytes:
        try:
            page = self._page(scripts=False)
        except PlaywrightError as error:
            raise RenderFailed(f"the email body could not be rendered: {error.message}") from error
        try:
            page.route("**/*", _only_inline_data)
            page.set_content(html, wait_until="load")
            return page.pdf(format="A4", print_background=True)
        except PlaywrightError as error:
            raise RenderFailed(f"the email body could not be rendered: {error.message}") from error
        finally:
            with suppress(PlaywrightError):
                page.close()

    def fetch(self, url: str) -> bytes | LoginGated:
        try:
            page = self._page(scripts=True)
        except PlaywrightError as error:
            raise PortalFetchFailed(f"could not open {url}: {error.message}") from error
        try:
            return self._fetch(page, url)
        except PlaywrightError as error:
            raise PortalFetchFailed(f"could not open {url}: {error.message}") from error
        finally:
            with suppress(PlaywrightError):
                page.close()

    def _fetch(self, page: Page, url: str) -> bytes | LoginGated:
        visit = _Visit()
        page.route("**/*", lambda route: self._guard(page, visit, route))

        # A redirect of the page itself is followed here, one step at a time, so each
        # destination is checked before it is requested.
        for _ in range(_MAX_REDIRECTS + 1):
            refusal = self._policy.refusal(url)
            if refusal:
                raise PortalFetchFailed(f"could not open {url}: {refusal}")

            visit.clear()
            page.goto(url, wait_until="load")
            if visit.redirect:
                url = visit.redirect
                continue
            if visit.status in (401, 403):
                return LoginGated()
            if visit.status is None or visit.status >= 400:
                raise PortalFetchFailed(f"could not open {url}: HTTP {visit.status}")
            if visit.pdf is not None:
                return visit.pdf

            self._settle(page)
            if page.locator(_SIGN_IN_FIELD).count() > 0:
                return LoginGated()
            return page.pdf(format="A4", print_background=True)
        raise PortalFetchFailed(f"could not open {url}: too many redirects")

    def _settle(self, page: Page) -> None:
        with suppress(PlaywrightTimeout):
            page.wait_for_load_state("networkidle", timeout=self._settle_ms)

    def _guard(self, page: Page, visit: _Visit, route: Route) -> None:
        """Applied to everything the page requests."""
        try:
            self._answer(page, visit, route)
        except PlaywrightError:
            # The request could not be made, or the page closed while it was under way.
            with suppress(PlaywrightError):
                route.abort("failed")

    def _answer(self, page: Page, visit: _Visit, route: Route) -> None:
        request = route.request
        if self._policy.refusal(request.url):
            route.abort("blockedbyclient")
            return

        response = route.fetch(max_redirects=0)
        if request.is_navigation_request() and request.frame == page.main_frame:
            visit.status = response.status
            visit.redirect = _redirect_target(request.url, response)
            if visit.redirect:
                route.fulfill(status=200, content_type="text/html", body=_BLANK)
            elif "application/pdf" in response.headers.get("content-type", ""):
                visit.pdf = response.body()
                route.fulfill(status=200, content_type="text/html", body=_BLANK)
            else:
                route.fulfill(response=response)
            return

        # Something the page asked for. Its redirects are followed only to allowed places.
        requested = request.url
        for _ in range(_MAX_REDIRECTS):
            target = _redirect_target(requested, response)
            if target is None:
                route.fulfill(response=response)
                return
            if self._policy.refusal(target):
                break
            requested = target
            response = route.fetch(url=target, max_redirects=0)
        route.abort("blockedbyclient")


def _only_inline_data(route: Route) -> None:
    if route.request.url.startswith("data:"):
        route.continue_()
    else:
        route.abort("blockedbyclient")


class Browser(Renderer, PortalFetcher, Protocol):
    """Renders email bodies and fetches portal pages."""


# Opens the browser a collection renders and fetches with.
BrowserFactory = Callable[[DestinationPolicy], AbstractContextManager[Browser]]


class ThreadConfinedBrowser:
    """A browser kept on a thread of its own, used from any thread one call at a time.

    Playwright's sync API may only be used from the thread that started it; a call
    from another thread fails even when calls never overlap. So the browser is started,
    used and stopped on one thread, and every call is handed to that thread. A run that
    examines several emails at once opens its browser this way.
    """

    def __init__(
        self, policy: DestinationPolicy, browser: BrowserFactory = HeadlessBrowser
    ) -> None:
        self._opening = browser(policy)
        self._thread = ThreadPoolExecutor(max_workers=1, thread_name_prefix="browser")

    def __enter__(self) -> Self:
        try:
            self._browser: Browser = self._thread.submit(self._opening.__enter__).result()
        except BaseException:
            # Nothing opened, so there is nothing to close: the thread alone is let go,
            # since __exit__ is not called for a with statement that could not begin.
            self._thread.shutdown()
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            self._thread.submit(self._opening.__exit__, exc_type, exc, traceback).result()
        finally:
            self._thread.shutdown()

    def render_html(self, html: str) -> bytes:
        return self._thread.submit(self._browser.render_html, html).result()

    def fetch(self, url: str) -> bytes | LoginGated:
        return self._thread.submit(self._browser.fetch, url).result()
