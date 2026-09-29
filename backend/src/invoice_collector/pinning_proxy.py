"""A proxy on this machine that every connection made by the browser passes through.

Checking a link before opening it is not enough. The name in the link is looked up once
when it is checked and again when the browser connects, and whoever controls the name
can answer differently the second time, with an address inside the network.

The proxy closes that gap. It looks the name up itself, refuses any address that is not
public, and connects to the very address it checked.
"""

import ipaddress
import select
import socket
import socketserver
import threading
from collections.abc import Callable, Sequence
from contextlib import suppress
from types import TracebackType
from typing import Self

IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolve = Callable[[str, int], Sequence[IpAddress]]
Allows = Callable[[IpAddress], bool]

_BUFFER = 65536
_CONNECT_TIMEOUT = 15
_IDLE_TIMEOUT = 60


def resolve_by_dns(host: str, port: int) -> Sequence[IpAddress]:
    found = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [ipaddress.ip_address(info[4][0]) for info in found]


def is_public(address: IpAddress) -> bool:
    return address.is_global


class PinningProxy:
    """Tunnels secure connections to public addresses, and refuses everything else."""

    def __init__(self, resolve: Resolve = resolve_by_dns, allows: Allows = is_public) -> None:
        self._resolve = resolve
        self._allows = allows
        self._server: socketserver.ThreadingTCPServer | None = None
        self.refused: list[str] = []

    @property
    def address(self) -> str:
        assert self._server is not None, "the proxy has not been started"
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> Self:
        proxy = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                proxy.serve(self.request)

        socketserver.ThreadingTCPServer.daemon_threads = True
        self._server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    def serve(self, client: socket.socket) -> None:
        client.settimeout(_CONNECT_TIMEOUT)
        try:
            head = _read_head(client)
        except OSError:
            return
        words = head.split(b"\r\n", 1)[0].split()
        if len(words) < 2 or words[0] != b"CONNECT":
            self._refuse(client, "only secure connections are made")
            return

        host, _, port_text = words[1].decode("ascii", "replace").rpartition(":")
        host = host.strip("[]")
        if not host or not port_text.isdigit():
            self._refuse(client, "the address is malformed")
            return

        upstream = self._connect(host, int(port_text))
        if upstream is None:
            self._refuse(client, f"{host} is not a public address")
            return
        with upstream, suppress(OSError):
            client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            _relay(client, upstream)

    def _connect(self, host: str, port: int) -> socket.socket | None:
        try:
            addresses = list(self._resolve(host, port))
        except (OSError, UnicodeError, ValueError):
            return None
        # One address that is not public is enough to refuse: a name that answers with
        # both kinds is not to be trusted with either.
        if not addresses or not all(self._allows(address) for address in addresses):
            return None
        for address in addresses:
            try:
                return socket.create_connection((str(address), port), timeout=_CONNECT_TIMEOUT)
            except OSError:
                continue
        return None

    def _refuse(self, client: socket.socket, reason: str) -> None:
        # Answered as a failed gateway, not as "forbidden": a refusal by this proxy must
        # not be mistaken for a portal that wants a sign-in.
        self.refused.append(reason)
        body = reason.encode()
        head = (
            "HTTP/1.1 502 Bad Gateway\r\nContent-Type: text/plain\r\n"
            f"Connection: close\r\nContent-Length: {len(body)}\r\n\r\n"
        )
        with suppress(OSError):
            client.sendall(head.encode() + body)


def _read_head(client: socket.socket) -> bytes:
    head = b""
    while b"\r\n\r\n" not in head and len(head) < _BUFFER:
        chunk = client.recv(_BUFFER)
        if not chunk:
            break
        head += chunk
    return head


def _relay(client: socket.socket, upstream: socket.socket) -> None:
    pair = {client: upstream, upstream: client}
    while True:
        ready, _, broken = select.select(list(pair), [], list(pair), _IDLE_TIMEOUT)
        if broken or not ready:
            return
        for source in ready:
            data = source.recv(_BUFFER)
            if not data:
                return
            pair[source].sendall(data)
