"""The headless browser against local pages. These drive a real browser."""

import ipaddress
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from invoice_collector.browser import HeadlessBrowser
from invoice_collector.destinations import DestinationPolicy
from invoice_collector.pinning_proxy import PinningProxy
from invoice_collector.portal import LoginGated, PortalFetchFailed
from invoice_collector.renderer import RenderFailed

pytestmark = pytest.mark.browser

INVOICE = "<h1>Figma, Inc.</h1><p>Invoice FIG-55102</p><p>Amount due US$190.00</p>"
SIGN_IN = '<form><input type="email" name="email"><input type="password" name="password"></form>'
DIRECT_PDF = b"%PDF-1.7 served directly"


@dataclass
class Site:
    """A local web server that remembers what was asked of it."""

    pages: dict[str, str] = field(default_factory=dict[str, str])
    redirects: dict[str, str] = field(default_factory=dict[str, str])
    answers_once: set[str] = field(default_factory=set[str])
    requested: list[str] = field(default_factory=list[str])
    url: str = ""

    def start(self) -> ThreadingHTTPServer:
        site = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                pass

            def _send(self, status: int, content_type: str, body: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                asked_before = self.path in site.requested
                site.requested.append(self.path)
                if self.path in site.answers_once and asked_before:
                    self._send(403, "text/plain", b"this link has been used")
                elif self.path == "/logo.png":
                    self._send(200, "image/png", b"not really a picture")
                elif self.path == "/poll":
                    self._send(200, "application/json", b"{}")
                elif self.path in site.redirects:
                    self.send_response(302)
                    self.send_header("Location", site.redirects[self.path])
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                elif self.path in site.pages:
                    self._send(200, "text/html; charset=utf-8", site.pages[self.path].encode())
                elif self.path == "/direct.pdf":
                    self._send(200, "application/pdf", DIRECT_PDF)
                elif self.path == "/forbidden":
                    self._send(403, "text/plain", b"forbidden")
                else:
                    self._send(404, "text/plain", b"not found")

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{server.server_port}"
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server


@pytest.fixture
def sites() -> Iterator[tuple[Site, Site]]:
    """The vendor's portal, and another machine that nothing should reach."""
    portal, other = Site(), Site()
    servers = [portal.start(), other.start()]
    yield portal, other
    for server in servers:
        server.shutdown()
        server.server_close()


@pytest.fixture
def portal(sites: tuple[Site, Site]) -> Site:
    portal, other = sites
    portal.pages = {
        "/in_1PqX7fK2.html": INVOICE,
        "/login.html": SIGN_IN,
        "/once.html": INVOICE,
        "/with-logo.html": f'{INVOICE}<img src="/logo-moved"><img src="/logo-elsewhere">',
        "/never-quiet.html": f"{INVOICE}<script>setInterval(() => fetch('/poll'), 100)</script>",
        "/with-tracker.html": f'{INVOICE}<img src="{other.url}/pixel.png">'
        f'<script>fetch("{other.url}/beacon")</script>',
    }
    portal.redirects = {
        "/moved": "/in_1PqX7fK2.html",
        "/loop": "/loop",
        "/logo-moved": "/logo.png",
        "/logo-elsewhere": f"{other.url}/logo.png",
        "/elsewhere": f"{other.url}/secret",
    }
    portal.answers_once = {"/once.html"}
    return portal


@pytest.fixture
def other(sites: tuple[Site, Site]) -> Site:
    return sites[1]


@dataclass(frozen=True)
class OnlyThePortal(DestinationPolicy):
    """Allows the portal's address and refuses every other."""

    portal_url: str = ""
    # The portal in these tests is on this machine.
    allow_private_addresses: bool = True

    def refusal(self, url: str) -> str | None:
        return None if url.startswith(self.portal_url) else "not the portal"


@dataclass(frozen=True)
class FooledByTheName(DestinationPolicy):
    """Stands for a check that passed because the name answered with a public address."""

    def refusal(self, url: str) -> str | None:
        return None


@pytest.fixture
def browser(portal: Site) -> Iterator[HeadlessBrowser]:
    with HeadlessBrowser(OnlyThePortal(portal_url=portal.url), settle_ms=500) as browser:
        yield browser


# Rendering an email body


def test_email_body_is_rendered_to_a_pdf(browser: HeadlessBrowser) -> None:
    rendered = browser.render_html(INVOICE)

    assert rendered.startswith(b"%PDF-")


def test_rendering_an_email_body_reaches_no_other_machine(
    browser: HeadlessBrowser, portal: Site, other: Site
) -> None:
    hostile = (
        f'{INVOICE}<img src="{other.url}/pixel.png"><img src="{portal.url}/pixel.png">'
        f'<link rel="stylesheet" href="{other.url}/style.css">'
        f'<script>fetch("{other.url}/beacon")</script>'
        f'<iframe src="{other.url}/frame"></iframe>'
    )

    rendered = browser.render_html(hostile)

    assert rendered.startswith(b"%PDF-")
    assert other.requested == []
    assert portal.requested == []


def test_images_carried_inside_the_email_are_still_rendered(browser: HeadlessBrowser) -> None:
    pixel = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"

    rendered = browser.render_html(f'{INVOICE}<img src="{pixel}">')

    assert rendered.startswith(b"%PDF-")


# Fetching a portal link


def test_tokenised_portal_page_is_returned_as_a_pdf(browser: HeadlessBrowser, portal: Site) -> None:
    fetched = browser.fetch(f"{portal.url}/in_1PqX7fK2.html")

    assert isinstance(fetched, bytes)
    assert fetched.startswith(b"%PDF-")


def test_portal_link_to_a_pdf_returns_that_pdf(browser: HeadlessBrowser, portal: Site) -> None:
    assert browser.fetch(f"{portal.url}/direct.pdf") == DIRECT_PDF


def test_page_asking_for_a_password_is_login_gated(browser: HeadlessBrowser, portal: Site) -> None:
    assert browser.fetch(f"{portal.url}/login.html") == LoginGated()


def test_page_that_refuses_access_is_login_gated(browser: HeadlessBrowser, portal: Site) -> None:
    assert browser.fetch(f"{portal.url}/forbidden") == LoginGated()


def test_missing_page_fails(browser: HeadlessBrowser, portal: Site) -> None:
    with pytest.raises(PortalFetchFailed, match="HTTP 404"):
        browser.fetch(f"{portal.url}/nothing-here")


def test_redirect_to_an_allowed_address_is_followed(browser: HeadlessBrowser, portal: Site) -> None:
    fetched = browser.fetch(f"{portal.url}/moved")

    assert isinstance(fetched, bytes)
    assert fetched.startswith(b"%PDF-")


def test_redirect_to_an_address_that_is_not_allowed_is_refused(
    browser: HeadlessBrowser, portal: Site, other: Site
) -> None:
    with pytest.raises(PortalFetchFailed, match="not the portal"):
        browser.fetch(f"{portal.url}/elsewhere")

    assert other.requested == []


def test_endless_redirect_fails(browser: HeadlessBrowser, portal: Site) -> None:
    with pytest.raises(PortalFetchFailed, match="too many redirects"):
        browser.fetch(f"{portal.url}/loop")


def test_link_to_an_address_that_is_not_allowed_is_never_requested(
    browser: HeadlessBrowser, other: Site
) -> None:
    with pytest.raises(PortalFetchFailed, match="not the portal"):
        browser.fetch(f"{other.url}/secret")

    assert other.requested == []


def test_what_a_portal_page_requests_is_held_to_the_same_policy(
    browser: HeadlessBrowser, portal: Site, other: Site
) -> None:
    fetched = browser.fetch(f"{portal.url}/with-tracker.html")

    assert isinstance(fetched, bytes)
    assert other.requested == []


def test_by_default_a_link_to_this_machine_is_refused(portal: Site) -> None:
    with HeadlessBrowser() as strict, pytest.raises(PortalFetchFailed, match="http links"):
        strict.fetch(f"{portal.url}/in_1PqX7fK2.html")

    assert portal.requested == []


def test_the_sample_portal_named_as_a_setting_is_reached_through_the_strict_browser(
    portal: Site, other: Site
) -> None:
    policy = DestinationPolicy.with_sample_portal(portal.url)

    with HeadlessBrowser(policy, settle_ms=500) as strict:
        pdf = strict.fetch(f"{portal.url}/with-tracker.html")
        with pytest.raises(PortalFetchFailed, match="http links"):
            strict.fetch(f"{other.url}/in_1PqX7fK2.html")

    assert isinstance(pdf, bytes) and pdf.startswith(b"%PDF-")
    assert portal.requested == ["/with-tracker.html"]
    # The other machine, on the same host but another port, is reached neither by the link
    # nor by what the portal page asks for.
    assert other.requested == []


def test_link_that_answers_only_once_is_requested_only_once(
    browser: HeadlessBrowser, portal: Site
) -> None:
    fetched = browser.fetch(f"{portal.url}/once.html")

    assert isinstance(fetched, bytes)
    assert fetched.startswith(b"%PDF-")
    assert portal.requested.count("/once.html") == 1


def test_link_to_a_pdf_is_requested_only_once(browser: HeadlessBrowser, portal: Site) -> None:
    browser.fetch(f"{portal.url}/direct.pdf")

    assert portal.requested.count("/direct.pdf") == 1


def test_picture_that_has_moved_within_the_portal_is_still_loaded(
    browser: HeadlessBrowser, portal: Site, other: Site
) -> None:
    fetched = browser.fetch(f"{portal.url}/with-logo.html")

    assert isinstance(fetched, bytes)
    assert "/logo.png" in portal.requested
    assert other.requested == []


def test_page_that_never_goes_quiet_is_still_collected(
    browser: HeadlessBrowser, portal: Site
) -> None:
    fetched = browser.fetch(f"{portal.url}/never-quiet.html")

    assert isinstance(fetched, bytes)
    assert fetched.startswith(b"%PDF-")


def test_name_that_changes_its_answer_after_the_check_is_not_reached(other: Site) -> None:
    port = other.url.rsplit(":", 1)[1]
    proxy = PinningProxy(resolve=lambda host, port: [ipaddress.ip_address("127.0.0.1")])

    with (
        HeadlessBrowser(FooledByTheName(), proxy=proxy) as browser,
        pytest.raises(PortalFetchFailed),
    ):
        browser.fetch(f"https://rebound.attacker.example:{port}/secret")

    assert proxy.refused == ["rebound.attacker.example is not a public address"]
    assert other.requested == []


def test_rendering_that_cannot_be_done_is_reported_as_a_failure_to_render() -> None:
    with HeadlessBrowser(DestinationPolicy.for_local_pages()) as closed:
        pass

    with pytest.raises(RenderFailed, match="could not be rendered"):
        closed.render_html(INVOICE)
