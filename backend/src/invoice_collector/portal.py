"""Fetches the billing document behind a portal link."""

from collections.abc import Mapping
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


class FakePortalFetcher:
    def __init__(self, pages: Mapping[str, bytes | LoginGated]) -> None:
        self._pages = dict(pages)

    def fetch(self, url: str) -> bytes | LoginGated:
        try:
            return self._pages[url]
        except KeyError:
            raise PortalFetchFailed(f"could not open {url}") from None
