"""Where emails come from."""

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from invoice_collector.domain import Email


class SourceAccountUnavailable(Exception):
    """The mail of a source account could not be read."""


class MailSource(Protocol):
    @property
    def source_account(self) -> str: ...

    def emails_between(self, start: datetime, end: datetime) -> Sequence[Email]:
        """Emails received from start up to, but not including, end.

        Raises SourceAccountUnavailable.
        """
        ...


class InMemoryMailSource:
    def __init__(self, source_account: str, emails: Sequence[Email]) -> None:
        self._source_account = source_account
        self._emails = tuple(emails)

    @property
    def source_account(self) -> str:
        return self._source_account

    def emails_between(self, start: datetime, end: datetime) -> Sequence[Email]:
        return [e for e in self._emails if start <= e.received_at < end]
