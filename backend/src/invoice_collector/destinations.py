"""Which addresses the tool may open when it follows a link taken from an email.

The sender of an email chooses the link, so the link is not trusted. Left unchecked, it
could point the tool at a machine on the local network, or at the machine it runs on.
"""

import ipaddress
import socket
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from urllib.parse import SplitResult, urlsplit, urlunsplit

# Where the sample portal is served inside Compose, when its pages are to be reachable.
SAMPLE_PORTAL_URL_VARIABLE = "INVOICE_COLLECTOR_SAMPLE_PORTAL_URL"
# The addresses the links of sample mail already in a mailbox were written with, such as
# http://localhost:8765, the sample generator's own. A link written with exactly one of them
# is opened at the sample portal's address instead.
SAMPLE_PORTAL_LINKS_VARIABLE = "INVOICE_COLLECTOR_SAMPLE_PORTAL_LINKS"
_DEFAULT_PORTS = {"http": 80, "https": 443}

Origin = tuple[str, str, int]


class NotAnOrigin(ValueError):
    """An address given for the sample portal is not a scheme, a host and a port alone, or
    the sample portal's settings do not go together."""


def _origin(parts: SplitResult) -> Origin | None:
    try:
        port = parts.port or _DEFAULT_PORTS.get(parts.scheme)
    except ValueError:
        return None
    if not parts.hostname or port is None:
        return None
    return parts.scheme, parts.hostname.lower(), port


def _is_origin(text: str) -> Origin | None:
    parts = urlsplit(text.strip())
    origin = _origin(parts)
    if (
        origin is None
        or parts.scheme not in _DEFAULT_PORTS
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
        or "@" in parts.netloc
    ):
        return None
    return origin


def parse_origin(text: str) -> Origin:
    """The scheme, host and port of an address such as http://portal:8765, and nothing more."""
    origin = _is_origin(text)
    if origin is None:
        raise NotAnOrigin(
            f"{SAMPLE_PORTAL_URL_VARIABLE} must be the sample portal's address alone, such as "
            f"http://portal:8765, with no path; it is {text.strip()}"
        )
    return origin


def parse_origins(text: str) -> frozenset[Origin]:
    """The addresses sample mail's links were written with, separated by commas."""
    origins: set[Origin] = set()
    for each in (part.strip() for part in text.split(",")):
        if not each:
            continue
        origin = _is_origin(each)
        if origin is None:
            raise NotAnOrigin(
                f"{SAMPLE_PORTAL_LINKS_VARIABLE} must be the addresses the links of sample "
                "mail were written with, each a scheme, a host and a port alone, such as "
                f"http://localhost:8765, separated by commas; {each} is not one"
            )
        origins.add(origin)
    return frozenset(origins)


def sample_portal_problems(environ: Mapping[str, str]) -> list[str]:
    """Every problem with the sample portal's settings, each a sentence of its own."""
    problems: list[str] = []
    portal = environ.get(SAMPLE_PORTAL_URL_VARIABLE, "").strip()
    links = environ.get(SAMPLE_PORTAL_LINKS_VARIABLE, "").strip()
    if portal:
        try:
            parse_origin(portal)
        except NotAnOrigin as problem:
            problems.append(f"{problem}.")
    if links:
        try:
            parse_origins(links)
        except NotAnOrigin as problem:
            problems.append(f"{problem}.")
        if not portal:
            problems.append(
                f"{SAMPLE_PORTAL_LINKS_VARIABLE} is set but {SAMPLE_PORTAL_URL_VARIABLE} is "
                "not. Links written with those addresses are opened at the sample portal's "
                f"address, so give that too, such as {SAMPLE_PORTAL_URL_VARIABLE}="
                "http://portal:8765, or leave both unset."
            )
    return problems


@dataclass(frozen=True)
class DestinationPolicy:
    schemes: frozenset[str] = frozenset({"https"})
    allow_private_addresses: bool = False
    # The one address of the sample portal served beside the tool, as scheme, host and port,
    # that may be opened although it is on the local network. Only for sample mail.
    sample_portal: Origin | None = None
    # The addresses sample mail's links were written with. A link written with exactly one
    # of them is opened at the sample portal's address. None is ever opened itself.
    sample_portal_links: frozenset[Origin] = frozenset()

    @classmethod
    def for_local_pages(cls) -> "DestinationPolicy":
        """For sample portal pages served from this machine. Never for real mail."""
        return cls(schemes=frozenset({"http", "https"}), allow_private_addresses=True)

    @classmethod
    def with_sample_portal(cls, address: str, links: Sequence[str] = ()) -> "DestinationPolicy":
        """Public secure addresses, and the sample portal at exactly this address.

        Nothing else on the local network is opened: not another port of the same host, not
        another host. A link written with one of the links' addresses is opened at the sample
        portal's address. Raises NotAnOrigin when an address is more than a scheme, a host
        and a port.
        """
        return cls(
            sample_portal=parse_origin(address),
            sample_portal_links=parse_origins(",".join(links)),
        )

    @classmethod
    def from_environment(cls, environ: Mapping[str, str]) -> "DestinationPolicy":
        """The policy the sample portal's settings give: secure public addresses alone when
        they are unset. Raises NotAnOrigin naming every problem with them."""
        problems = sample_portal_problems(environ)
        if problems:
            raise NotAnOrigin(" ".join(problems))
        portal = environ.get(SAMPLE_PORTAL_URL_VARIABLE, "").strip()
        if not portal:
            return cls()
        links = environ.get(SAMPLE_PORTAL_LINKS_VARIABLE, "").split(",")
        return cls.with_sample_portal(portal, links)

    def opening_local_pages(self) -> "DestinationPolicy":
        """This policy, also opening anything on this machine and the local network."""
        return replace(self, schemes=frozenset({"http", "https"}), allow_private_addresses=True)

    @property
    def sample_portal_host(self) -> str | None:
        return self.sample_portal[1] if self.sample_portal is not None else None

    def address_to_open(self, url: str) -> str:
        """Where a link from an email is opened: at the sample portal, for a link written with
        exactly one of the addresses sample mail's links were written with, the path and
        query kept; otherwise where it leads, to be checked as any link is.

        A link carrying a user name or password is left as it is, and so refused.
        """
        if self.sample_portal is None or not self.sample_portal_links:
            return url
        try:
            parts = urlsplit(url)
            written = _origin(parts)
        except ValueError:
            return url
        if written not in self.sample_portal_links or "@" in parts.netloc:
            return url
        scheme, host, port = self.sample_portal
        netloc = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        return urlunsplit((scheme, netloc, parts.path, parts.query, ""))

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
