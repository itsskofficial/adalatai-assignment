"""Shared fixtures."""

import json
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import anthropic
import pytest
from support import Collection

from invoice_collector.ledger import Ledger


@pytest.fixture(autouse=True)
def no_live_services(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may reach a live service, whatever keys this machine holds."""
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "JEV_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("INVOICE_COLLECTOR_SKIP_DOTENV", "1")


ReplayServer = Callable[[int, dict[str, Any]], str]
ReplayClient = Callable[[int, dict[str, Any]], anthropic.Anthropic]


@pytest.fixture
def replay_server() -> Iterator[ReplayServer]:
    """Starts a local server replaying one recorded response, and gives its address."""
    servers: list[ThreadingHTTPServer] = []

    def start(status: int, body: dict[str, Any]) -> str:
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                pass

            def do_POST(self) -> None:
                self.rfile.read(int(self.headers["Content-Length"]))
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{server.server_port}"

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


@pytest.fixture
def replay_client(replay_server: ReplayServer) -> ReplayClient:
    """A client for Claude that talks to a local server replaying one recorded response."""
    return lambda status, body: anthropic.Anthropic(
        api_key="not-a-real-key", base_url=replay_server(status, body), max_retries=0
    )


@pytest.fixture
def collection(tmp_path: Path) -> Iterator[Collection]:
    """A collection that can be run against emails given in the test."""
    ledger = Ledger(tmp_path / "ledger.sqlite")
    yield Collection(tmp_path, ledger)
    ledger.close()
