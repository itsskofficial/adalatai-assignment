"""Serves the sample portal pages, so the portal links in the sample emails open."""

import contextlib
import functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEFAULT_PORT = 8765
# This machine only, unless told: in Compose the portal is a service of its own, reached by
# the runner over the private network.
DEFAULT_HOST = "127.0.0.1"


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


def portal_server(
    folder: Path, port: int = DEFAULT_PORT, host: str = DEFAULT_HOST
) -> ThreadingHTTPServer:
    """A server for the files in folder, on this machine only unless another address is
    given. Port 0 picks a free port."""
    handler = functools.partial(_QuietHandler, directory=str(folder))
    return ThreadingHTTPServer((host, port), handler)


def serve_until_interrupted(server: ThreadingHTTPServer) -> None:
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()
