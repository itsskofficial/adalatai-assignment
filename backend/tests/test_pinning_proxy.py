"""The proxy that checks an address at the moment of connection."""

import ipaddress
import socket
import socketserver
import threading
from collections.abc import Iterator, Sequence

import pytest

from invoice_collector.pinning_proxy import IpAddress, PinningProxy, is_public

LOOPBACK = ipaddress.ip_address("127.0.0.1")


class Echo(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        data = self.request.recv(1024)
        self.request.sendall(b"echo:" + data)


@pytest.fixture
def vendor() -> Iterator[int]:
    """A machine standing in for the vendor's portal. Gives its port."""
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Echo)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def ask(proxy: PinningProxy, request: bytes) -> tuple[bytes, socket.socket]:
    host, port = proxy.address.removeprefix("http://").split(":")
    connection = socket.create_connection((host, int(port)), timeout=5)
    connection.sendall(request)
    return connection.recv(4096), connection


def resolving_to(*addresses: str):
    def resolve(host: str, port: int) -> Sequence[IpAddress]:
        return [ipaddress.ip_address(a) for a in addresses]

    return resolve


def test_connection_is_made_to_the_address_that_was_checked(vendor: int) -> None:
    looked_up: list[str] = []

    def resolve(host: str, port: int) -> Sequence[IpAddress]:
        looked_up.append(host)
        return [LOOPBACK]

    with PinningProxy(resolve, allows=lambda address: True) as proxy:
        answer, tunnel = ask(
            proxy, f"CONNECT portal.vendor.example:{vendor} HTTP/1.1\r\n\r\n".encode()
        )
        assert answer.startswith(b"HTTP/1.1 200")
        tunnel.sendall(b"hello")
        assert tunnel.recv(1024) == b"echo:hello"
        tunnel.close()

    assert looked_up == ["portal.vendor.example"]


def test_name_that_answers_with_an_address_inside_the_network_is_refused(vendor: int) -> None:
    with PinningProxy(resolving_to("127.0.0.1")) as proxy:
        answer, connection = ask(
            proxy, f"CONNECT rebound.attacker.example:{vendor} HTTP/1.1\r\n\r\n".encode()
        )
        connection.close()

    assert answer.startswith(b"HTTP/1.1 502")
    assert proxy.refused == ["rebound.attacker.example is not a public address"]


def test_name_that_answers_with_both_kinds_of_address_is_refused(vendor: int) -> None:
    with PinningProxy(resolving_to("8.8.8.8", "10.0.0.5")) as proxy:
        answer, connection = ask(proxy, f"CONNECT mixed.example:{vendor} HTTP/1.1\r\n\r\n".encode())
        connection.close()

    assert answer.startswith(b"HTTP/1.1 502")


def test_name_that_cannot_be_found_is_refused() -> None:
    def resolve(host: str, port: int) -> Sequence[IpAddress]:
        raise OSError("no such host")

    with PinningProxy(resolve) as proxy:
        answer, connection = ask(proxy, b"CONNECT nowhere.invalid:443 HTTP/1.1\r\n\r\n")
        connection.close()

    assert answer.startswith(b"HTTP/1.1 502")


def test_connection_that_is_not_secure_is_refused() -> None:
    with PinningProxy(resolving_to("8.8.8.8")) as proxy:
        answer, connection = ask(
            proxy, b"GET http://portal.vendor.example/invoice HTTP/1.1\r\nHost: x\r\n\r\n"
        )
        connection.close()

    assert answer.startswith(b"HTTP/1.1 502")
    assert proxy.refused == ["only secure connections are made"]


def test_malformed_address_is_refused() -> None:
    with PinningProxy(resolving_to("8.8.8.8")) as proxy:
        answer, connection = ask(proxy, b"CONNECT no-port-here HTTP/1.1\r\n\r\n")
        connection.close()

    assert answer.startswith(b"HTTP/1.1 502")


@pytest.mark.parametrize(
    ("address", "public"),
    [
        ("8.8.8.8", True),
        ("127.0.0.1", False),
        ("10.0.0.5", False),
        ("192.168.1.10", False),
        ("169.254.169.254", False),
        ("::1", False),
        ("fd00::1", False),
    ],
)
def test_only_public_addresses_are_allowed_by_default(address: str, public: bool) -> None:
    assert is_public(ipaddress.ip_address(address)) is public
