"""The sample portal serves the portal pages, and those of each month set, and nothing else."""

import http.client
import threading
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from invoice_collector.seed.cli import main
from invoice_collector.seed.portal_server import portal_server

SAMPLES = Path(__file__).parents[1] / "samples"


@pytest.fixture
def samples(tmp_path: Path) -> Path:
    for path, text in {
        "portal/sign-in.html": "standard sign-in",
        "golden.json": "[]",
        "engineering@acme.example/a.eml": "Subject: a",
        "2026-09/portal/in_abc.html": "september page",
        "2026-09/portal/sign-in.html": "september sign-in",
        "2026-09/golden.json": "[]",
        "2026-09/engineering@acme.example/b.eml": "Subject: b",
        "2026-9/portal/in_abc.html": "not a month folder",
        "notes/portal/in_abc.html": "not a month folder",
    }.items():
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(text, encoding="utf-8")
    (tmp_path / "2026-09" / "portal" / "folder").mkdir()
    return tmp_path


@pytest.fixture
def server(samples: Path) -> Iterator[ThreadingHTTPServer]:
    with portal_server(samples, 0) as running:
        thread = threading.Thread(target=running.serve_forever, daemon=True)
        thread.start()
        yield running
        running.shutdown()
        thread.join(10)


def get(server: ThreadingHTTPServer, path: str, method: str = "GET") -> tuple[int, bytes]:
    """The path is sent exactly as given, never normalised by the client."""
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    try:
        connection.putrequest(method, path, skip_accept_encoding=True)
        connection.endheaders()
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("path", "text"),
    [
        ("/sign-in.html", "standard sign-in"),
        ("/2026-09/in_abc.html", "september page"),
        ("/2026-09/sign-in.html", "september sign-in"),
        ("/2026-09/in_abc.html?session=1", "september page"),
    ],
)
def test_a_page_of_the_portal_or_of_a_month_set_is_served(
    server: ThreadingHTTPServer, path: str, text: str
) -> None:
    assert get(server, path) == (200, text.encode())


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/2026-09",
        "/2026-09/",
        "/golden.json",
        "/2026-09/golden.json",
        "/portal/sign-in.html",
        "/2026-09/portal/sign-in.html",
        "/engineering@acme.example/a.eml",
        "/2026-09/engineering@acme.example/b.eml",
        "/2026-09/../golden.json",
        "/2026-09/../portal/sign-in.html",
        "/2026-09/%2e%2e/golden.json",
        "/%2e%2e/golden.json",
        "/..%2fgolden.json",
        "/2026-09/..%5cgolden.json",
        "/2026-09/folder",
        "/2026-10/in_abc.html",
        "/2026-9/in_abc.html",
        "/notes/in_abc.html",
        "/missing.html",
    ],
)
def test_nothing_else_of_the_samples_folder_is_served(
    server: ThreadingHTTPServer, path: str
) -> None:
    for method in ("GET", "HEAD"):
        status, _ = get(server, path, method)
        assert status == 404, (method, path)


def test_the_portal_command_serves_the_committed_september_pages(
    capsys: pytest.CaptureFixture[str],
) -> None:
    started: list[ThreadingHTTPServer] = []
    ready = threading.Event()

    def serve(server: ThreadingHTTPServer) -> None:
        started.append(server)
        ready.set()
        server.serve_forever()

    thread = threading.Thread(
        target=main,
        args=(["portal", "--samples", str(SAMPLES), "--port", "0"],),
        kwargs={"serve": serve},
        daemon=True,
    )
    thread.start()
    assert ready.wait(10)
    [server] = started
    try:
        september = get(server, "/2026-09/sign-in.html")
        standard = get(server, "/sign-in.html")
    finally:
        server.shutdown()
        thread.join(10)

    assert september == (200, (SAMPLES / "2026-09" / "portal" / "sign-in.html").read_bytes())
    assert standard == (200, (SAMPLES / "portal" / "sign-in.html").read_bytes())
    assert f"http://localhost:{server.server_port}/2026-09/" in capsys.readouterr().out
