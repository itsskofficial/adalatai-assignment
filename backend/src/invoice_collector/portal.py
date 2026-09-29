"""Fetches the billing document behind a portal link."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol


class PortalFetchFailed(Exception):
    pass


@dataclass(frozen=True)
class LoginGated:
    """The link leads to a sign-in page. A person has to fetch the document."""


class PortalFetcher(Protocol):
    def fetch(self, url: str) -> bytes | LoginGated:
        """Returns the document as a PDF, or reports that the link is login-gated."""
        ...


class OpenedWhereTheyLead:
    """A fetcher that opens each link at the address the policy gives for it.

    The link is whatever the email says, and it stays the document's identity in the
    ledger; only the address asked of the fetcher may differ. For sample mail written for a
    sample portal at another address. The fetcher still checks the address it is asked for.
    """

    def __init__(self, fetcher: PortalFetcher, address_to_open: Callable[[str], str]) -> None:
        self._fetcher = fetcher
        self._address_to_open = address_to_open

    def fetch(self, url: str) -> bytes | LoginGated:
        address = self._address_to_open(url)
        if address == url:
            return self._fetcher.fetch(url)
        try:
            return self._fetcher.fetch(address)
        except PortalFetchFailed as failure:
            raise PortalFetchFailed(f"{failure} (the link in the email is {url})") from failure


class FakePortalFetcher:
    def __init__(self, pages: Mapping[str, bytes | LoginGated]) -> None:
        self._pages = dict(pages)

    def fetch(self, url: str) -> bytes | LoginGated:
        try:
            return self._pages[url]
        except KeyError:
            raise PortalFetchFailed(f"could not open {url}") from None
