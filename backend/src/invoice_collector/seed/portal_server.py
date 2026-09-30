"""Serves the sample portal pages, so the portal links in the sample emails open."""

import contextlib
import functools
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from typing import BinaryIO
from urllib.parse import unquote, urlsplit

from invoice_collector.samples import PORTAL_FOLDER, is_month_folder

DEFAULT_PORT = 8765
# This machine only, unless told: in Compose the portal is a service of its own, reached by
# the runner over the private network.
DEFAULT_HOST = "127.0.0.1"


def portal_file(samples: Path, path: str) -> Path | None:
    """The page a request path names, or None: /<file> is a page of the portal folder, and
    /<YYYY-MM>/<file> one of that month's set. Nothing else of the samples folder is served."""
    *month, name = unquote(urlsplit(path).path).removeprefix("/").split("/")
    if name in ("", ".", "..") or any(c in name for c in "\\:\0"):
        return None
    if not month:
        folder = samples / PORTAL_FOLDER
    elif len(month) == 1 and is_month_folder(month[0]):
        folder = samples / month[0] / PORTAL_FOLDER
    else:
        return None
    file = folder / name
    return file if file.is_file() and file.resolve().parent == folder.resolve() else None


class _PortalHandler(SimpleHTTPRequestHandler):
    def translate_path(self, path: str) -> str:
        file = portal_file(Path(self.directory), path)
        return str(file) if file is not None else ""

    def send_head(self) -> BytesIO | BinaryIO | None:
        if portal_file(Path(self.directory), self.path) is None:
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return None
        return super().send_head()

    def log_message(self, format: str, *args: object) -> None:
        pass


def portal_server(
    samples: Path, port: int = DEFAULT_PORT, host: str = DEFAULT_HOST
) -> ThreadingHTTPServer:
    """A server for the portal pages of the samples folder, on this machine only unless
    another address is given. Port 0 picks a free port."""
    handler = functools.partial(_PortalHandler, directory=str(samples))
    return ThreadingHTTPServer((host, port), handler)


def serve_until_interrupted(server: ThreadingHTTPServer) -> None:
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()
