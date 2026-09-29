"""The headless browser against local pages. These drive a real browser."""

import threading
from collections.abc import Iterator
from functools import partial
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from invoice_collector.browser import HeadlessBrowser
from invoice_collector.portal import LoginGated, PortalFetchFailed

pytestmark = pytest.mark.browser

INVOICE = "<h1>Figma, Inc.</h1><p>Invoice FIG-55102</p><p>Amount due US$190.00</p>"
SIGN_IN = '<form><input type="email" name="email"><input type="password" name="password"></form>'
DIRECT_PDF = b"%PDF-1.7 served directly"


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        if self.path == "/forbidden":
            self.send_error(403)
        elif self.path == "/direct.pdf":
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.end_headers()
            self.wfile.write(DIRECT_PDF)
        else:
            super().do_GET()


@pytest.fixture
def portal(tmp_path: Path) -> Iterator[str]:
    (tmp_path / "in_1PqX7fK2.html").write_text(INVOICE, encoding="utf-8")
    (tmp_path / "login.html").write_text(SIGN_IN, encoding="utf-8")
    handler: type[BaseHTTPRequestHandler] = partial(  # pyright: ignore[reportAssignmentType]
        QuietHandler, directory=str(tmp_path)
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


@pytest.fixture(scope="module")
def browser() -> Iterator[HeadlessBrowser]:
    with HeadlessBrowser() as browser:
        yield browser


def test_email_body_is_rendered_to_a_pdf(browser: HeadlessBrowser) -> None:
    rendered = browser.render_html(INVOICE)

    assert rendered.startswith(b"%PDF-")


def test_tokenised_portal_page_is_returned_as_a_pdf(browser: HeadlessBrowser, portal: str) -> None:
    fetched = browser.fetch(f"{portal}/in_1PqX7fK2.html")

    assert isinstance(fetched, bytes)
    assert fetched.startswith(b"%PDF-")


def test_portal_link_to_a_pdf_returns_that_pdf(browser: HeadlessBrowser, portal: str) -> None:
    assert browser.fetch(f"{portal}/direct.pdf") == DIRECT_PDF


def test_page_asking_for_a_password_is_login_gated(browser: HeadlessBrowser, portal: str) -> None:
    assert browser.fetch(f"{portal}/login.html") == LoginGated()


def test_page_that_refuses_access_is_login_gated(browser: HeadlessBrowser, portal: str) -> None:
    assert browser.fetch(f"{portal}/forbidden") == LoginGated()


def test_missing_page_fails(browser: HeadlessBrowser, portal: str) -> None:
    with pytest.raises(PortalFetchFailed, match="HTTP 404"):
        browser.fetch(f"{portal}/nothing-here")
