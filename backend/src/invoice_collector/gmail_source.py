"""Reads a source account through Gmail, with read-only access. See ADR 0002."""

import base64
import math
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from google.auth.exceptions import RefreshError

from invoice_collector.domain import Email
from invoice_collector.google_auth import (
    DEFAULT_CLIENT_FILE,
    DEFAULT_TOKEN_DIR,
    GMAIL_READONLY,
    SignInExpired,
    gmail_service,
    sign_in,
)
from invoice_collector.mime import email_from_rfc822

SCOPES = (GMAIL_READONLY,)
DEFAULT_QUERY = (
    '(invoice OR receipt OR billing OR payment OR "credit note" OR refund OR renew OR subscription)'
)
_PAGE_SIZE = 500
_RETRIES = 3


class GmailMailSource:
    """The emails of one source account, found with a Gmail search.

    service is a Gmail API client, as built by google_auth.gmail_service.
    """

    def __init__(self, source_account: str, service: Any, query: str = DEFAULT_QUERY) -> None:
        self._source_account = source_account
        self._service = service
        self._query = query

    @classmethod
    def signed_in(
        cls,
        source_account: str,
        token_dir: Path = DEFAULT_TOKEN_DIR,
        client_file: Path = DEFAULT_CLIENT_FILE,
        query: str = DEFAULT_QUERY,
    ) -> "GmailMailSource":
        """The mail source using the stored sign-in. It never opens the browser."""
        credentials = sign_in(source_account, SCOPES, token_dir, client_file, allow_browser=False)
        return cls(source_account, gmail_service(credentials), query)

    @property
    def source_account(self) -> str:
        return self._source_account

    def emails_between(self, start: datetime, end: datetime) -> Sequence[Email]:
        """Emails received from start up to, but not including, end, oldest first."""
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("the window must carry a time zone")
        try:
            emails = [self._email(message_id) for message_id in self._message_ids(start, end)]
        except RefreshError as error:
            raise SignInExpired(self._source_account) from error
        # Gmail's date search is coarse, so the window is applied again here.
        return sorted(
            (e for e in emails if start <= e.received_at < end), key=lambda e: e.received_at
        )

    def _message_ids(self, start: datetime, end: datetime) -> list[str]:
        after, before = math.floor(start.timestamp()), math.ceil(end.timestamp())
        query = f"after:{after} before:{before} {self._query}"
        messages = self._service.users().messages()
        ids: list[str] = []
        page_token: str | None = None
        while True:
            page: dict[str, Any] = messages.list(
                userId="me", q=query, maxResults=_PAGE_SIZE, pageToken=page_token
            ).execute(num_retries=_RETRIES)
            ids.extend(str(message["id"]) for message in page.get("messages", []))
            page_token = page.get("nextPageToken")
            if not page_token:
                return ids

    def _email(self, message_id: str) -> Email:
        message: dict[str, Any] = (
            self._service.users()
            .messages()
            .get(userId="me", id=message_id, format="raw")
            .execute(num_retries=_RETRIES)
        )
        return email_from_rfc822(
            self._source_account,
            base64.urlsafe_b64decode(message["raw"]),
            message_id=str(message["id"]),
            received_at=datetime.fromtimestamp(int(message["internalDate"]) / 1000, tz=UTC),
        )
