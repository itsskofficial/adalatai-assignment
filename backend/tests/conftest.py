"""Shared fixtures."""

import json
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import anthropic
import pytest

ReplayClient = Callable[[int, dict[str, Any]], anthropic.Anthropic]


@pytest.fixture
def replay_client() -> Iterator[ReplayClient]:
    """A client for Claude that talks to a local server replaying one recorded response."""
    servers: list[ThreadingHTTPServer] = []

    def start(status: int, body: dict[str, Any]) -> anthropic.Anthropic:
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
        return anthropic.Anthropic(
            api_key="not-a-real-key",
            base_url=f"http://127.0.0.1:{server.server_port}",
            max_retries=0,
        )

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()
