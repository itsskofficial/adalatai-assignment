"""Which addresses the tool may open when it follows a link taken from an email.

The sender of an email chooses the link, so the link is not trusted. Left unchecked, it
could point the tool at a machine on the local network, or at the machine it runs on.
"""

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class DestinationPolicy:
    schemes: frozenset[str] = frozenset({"https"})
    allow_private_addresses: bool = False

    @classmethod
    def for_local_pages(cls) -> "DestinationPolicy":
        """For sample portal pages served from this machine. Never for real mail."""
        return cls(schemes=frozenset({"http", "https"}), allow_private_addresses=True)

    def refusal(self, url: str) -> str | None:
        """Why the address may not be opened, or None if it may."""
        try:
            parts = urlsplit(url)
            host, port = parts.hostname, parts.port
        except ValueError:
            return "the link is malformed"
        if parts.scheme not in self.schemes:
            return f"{parts.scheme or 'no scheme'} links are not followed"
        if not host:
            return "the link has no host"
        if parts.username or parts.password:
            return "links carrying a user name or password are not followed"
        if self.allow_private_addresses:
            return None

        try:
            found = socket.getaddrinfo(host, port or 443, type=socket.SOCK_STREAM)
            addresses = {ipaddress.ip_address(info[4][0]) for info in found}
        except (OSError, UnicodeError, ValueError):
            return f"{host} could not be found"
        if not addresses or not all(address.is_global for address in addresses):
            return f"{parts.hostname} is not a public address"
        return None
