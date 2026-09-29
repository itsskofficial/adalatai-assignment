"""Which addresses the tool may open when it follows a link taken from an email.

The sender of an email chooses the link, so the link is not trusted. Left unchecked, it
could point the tool at a machine on the local network, or at the machine it runs on.
"""

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import SplitResult, urlsplit

# Where the sample portal is served inside Compose, when its pages are to be reachable.
SAMPLE_PORTAL_URL_VARIABLE = "INVOICE_COLLECTOR_SAMPLE_PORTAL_URL"
_DEFAULT_PORTS = {"http": 80, "https": 443}


class NotAnOrigin(ValueError):
    """The address given for the sample portal is not a scheme, a host and a port alone."""


def _origin(parts: SplitResult) -> tuple[str, str, int] | None:
    try:
        port = parts.port or _DEFAULT_PORTS.get(parts.scheme)
    except ValueError:
        return None
    if not parts.hostname or port is None:
        return None
    return parts.scheme, parts.hostname.lower(), port


def parse_origin(text: str) -> tuple[str, str, int]:
    """The scheme, host and port of an address such as http://portal:8765, and nothing more."""
    parts = urlsplit(text.strip())
    origin = _origin(parts)
    if (
        origin is None
        or parts.scheme not in _DEFAULT_PORTS
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
        or parts.username
        or parts.password
    ):
        raise NotAnOrigin(
            f"{SAMPLE_PORTAL_URL_VARIABLE} must be the sample portal's address alone, such as "
            f"http://portal:8765, with no path; it is {text.strip()}"
        )
    return origin


@dataclass(frozen=True)
class DestinationPolicy:
    schemes: frozenset[str] = frozenset({"https"})
    allow_private_addresses: bool = False
    # The one address of the sample portal served beside the tool, as scheme, host and port,
    # that may be opened although it is on the local network. Only for sample mail.
    sample_portal: tuple[str, str, int] | None = None

    @classmethod
    def for_local_pages(cls) -> "DestinationPolicy":
        """For sample portal pages served from this machine. Never for real mail."""
        return cls(schemes=frozenset({"http", "https"}), allow_private_addresses=True)

    @classmethod
    def with_sample_portal(cls, address: str) -> "DestinationPolicy":
        """Public secure addresses, and the sample portal at exactly this address.

        Nothing else on the local network is opened: not another port of the same host, not
        another host. Raises NotAnOrigin when the address is more than a scheme, a host and a
        port.
        """
        return cls(sample_portal=parse_origin(address))

    @property
    def sample_portal_host(self) -> str | None:
        return self.sample_portal[1] if self.sample_portal is not None else None

    def refusal(self, url: str) -> str | None:
        """Why the address may not be opened, or None if it may."""
        try:
            parts = urlsplit(url)
            host, port = parts.hostname, parts.port
        except ValueError:
            return "the link is malformed"
        if (
            self.sample_portal is not None
            and not parts.username
            and not parts.password
            and _origin(parts) == self.sample_portal
        ):
            return None
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
