"""Sign-in for a Google account, stored on this machine.

Each account's sign-in is kept in its own file in a git-ignored folder. See ADR 0002.
"""

import contextlib
import json
import os
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build  # pyright: ignore[reportUnknownVariableType]

from invoice_collector.mail_source import SourceAccountUnavailable

GMAIL_READONLY = "https://www.googleapis.com/auth/gmail.readonly"
DRIVE_FILE = "https://www.googleapis.com/auth/drive.file"

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CLIENT_FILE = REPO_ROOT / "credentials" / "desktop-client.json"
DEFAULT_TOKEN_DIR = REPO_ROOT / "credentials" / "tokens"

BrowserFlow = Callable[[Path, Sequence[str]], Credentials]
GmailService = Callable[[Credentials], Any]


class SignInExpired(SourceAccountUnavailable):
    """The stored sign-in of an account no longer works: it expired or was revoked."""

    def __init__(self, source_account: str, reason: str = "it expired or was revoked") -> None:
        super().__init__(
            f"the sign-in for {source_account} no longer works ({reason}); "
            f"sign in again with: invoice-collector-setup {source_account}"
        )
        self.source_account = source_account


class NotSignedIn(SignInExpired):
    """The account has no usable stored sign-in, and the browser may not be opened."""

    def __init__(self, source_account: str) -> None:
        super().__init__(source_account, "no sign-in with the access needed is stored")


class WrongAccountSignedIn(Exception):
    """The person signed in to a different address than the one asked for."""

    def __init__(self, source_account: str, signed_in_address: str) -> None:
        super().__init__(
            f"expected a sign-in for {source_account}, but the browser signed in as "
            f"{signed_in_address}; nothing was stored"
        )
        self.source_account = source_account
        self.signed_in_address = signed_in_address


def browser_sign_in(client_file: Path, scopes: Sequence[str]) -> Credentials:
    """Opens the browser for the person to sign in, and waits for the answer locally."""
    if not client_file.is_file():
        raise FileNotFoundError(f"the OAuth desktop client file is missing: {client_file}")
    flow = InstalledAppFlow.from_client_secrets_file(  # pyright: ignore[reportUnknownMemberType]
        str(client_file), list(scopes)
    )
    return cast(
        Credentials,
        flow.run_local_server(port=0),  # pyright: ignore[reportUnknownMemberType]
    )


def gmail_service(credentials: Credentials) -> Any:
    """A Gmail client acting with the given sign-in."""
    return cast(Any, build("gmail", "v1", credentials=credentials, cache_discovery=False))


def sign_in(
    account: str,
    scopes: Sequence[str],
    token_dir: Path = DEFAULT_TOKEN_DIR,
    client_file: Path = DEFAULT_CLIENT_FILE,
    *,
    allow_browser: bool = True,
    renew: bool = False,
    browser_flow: BrowserFlow = browser_sign_in,
    gmail_service: GmailService = gmail_service,
) -> Credentials:
    """Authorised credentials for account, covering scopes.

    A stored sign-in is reused, and refreshed when it has run out. Without one, or
    when renew is set, the person signs in through the browser and the sign-in is
    stored. Raises SignInExpired when a refresh is refused, NotSignedIn when the
    browser is needed but not allowed, and WrongAccountSignedIn when the browser
    signed in to another address.
    """
    token_file = token_dir / f"{account}.json"
    stored = None if renew else _stored(token_file, scopes)
    if stored is not None:
        if stored.valid:
            return stored
        try:
            stored.refresh(Request())  # pyright: ignore[reportUnknownMemberType]
        except RefreshError as error:
            raise SignInExpired(account) from error
        _store(stored, token_file)
        return stored

    if not allow_browser:
        raise NotSignedIn(account)
    credentials = browser_flow(client_file, scopes)
    profile = gmail_service(credentials).users().getProfile(userId="me").execute()
    signed_in_address = str(profile["emailAddress"])
    if signed_in_address.casefold() != account.casefold():
        raise WrongAccountSignedIn(account, signed_in_address)
    _store(credentials, token_file)
    return credentials


def _stored(token_file: Path, scopes: Sequence[str]) -> Credentials | None:
    """The stored sign-in, if there is one that can be refreshed and covers scopes."""
    if not token_file.is_file():
        return None
    try:
        stored: dict[str, Any] = json.loads(token_file.read_text(encoding="utf-8"))
        granted: list[str] = list(stored["scopes"])
        if not stored["refresh_token"] or not set(scopes) <= set(granted):
            return None
        expiry = stored.get("expiry")
        return Credentials(  # pyright: ignore[reportUnknownVariableType]
            token=stored.get("token"),
            refresh_token=stored["refresh_token"],
            token_uri=stored["token_uri"],
            client_id=stored["client_id"],
            client_secret=stored["client_secret"],
            scopes=granted,
            # The library compares against a UTC time without a zone.
            expiry=datetime.fromisoformat(expiry).astimezone(UTC).replace(tzinfo=None)
            if expiry
            else None,
        )
    except (ValueError, KeyError, TypeError):
        return None


def _store(credentials: Credentials, token_file: Path) -> None:
    """Stores the sign-in so that only its owner can read it, at every moment.

    It is written to a file that is private from the instant it is created, which then
    takes the place of any earlier one. The file is never readable by others, and a
    sign-in is never left half written.
    """
    token_file.parent.mkdir(parents=True, exist_ok=True)
    content: str = credentials.to_json()  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    being_written = token_file.with_name(f"{token_file.name}.{os.getpid()}.part")
    descriptor = os.open(being_written, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(content)
        os.replace(being_written, token_file)
    finally:
        with contextlib.suppress(OSError):
            being_written.unlink()
