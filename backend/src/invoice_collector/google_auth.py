"""Sign-in for a Google account, stored on this machine.

Each account's sign-in is kept in its own file in a git-ignored folder. See ADR 0002.
"""

import contextlib
import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
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
# Only for putting sample emails into a mailbox; the pipeline never asks for it.
GMAIL_INSERT = "https://www.googleapis.com/auth/gmail.insert"
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


def drive_and_sheets_services(credentials: Credentials) -> tuple[Any, Any]:
    """Drive v3 and Sheets v4 clients acting with the given sign-in."""
    drive = cast(Any, build("drive", "v3", credentials=credentials, cache_discovery=False))
    sheets = cast(Any, build("sheets", "v4", credentials=credentials, cache_discovery=False))
    return drive, sheets


def sign_in(
    account: str,
    scopes: Sequence[str],
    token_dir: Path = DEFAULT_TOKEN_DIR,
    client_file: Path = DEFAULT_CLIENT_FILE,
    *,
    allow_browser: bool = True,
    renew: bool = False,
    purpose: str | None = None,
    check_address: bool = True,
    browser_flow: BrowserFlow = browser_sign_in,
    gmail_service: GmailService = gmail_service,
) -> Credentials:
    """Authorised credentials for account, covering scopes.

    A stored sign-in is reused, and refreshed when it has run out. Without one, or
    when renew is set, the person signs in through the browser and the sign-in is
    stored. Raises SignInExpired when a refresh is refused, NotSignedIn when the
    browser is needed but not allowed, and WrongAccountSignedIn when the browser
    signed in to another address.

    A sign-in for another purpose than reading, such as "seeding", is stored in its
    own file, so it never widens the access of the sign-in the pipeline reads with.
    check_address may be turned off only for scopes that cannot read the address.
    """
    token_file = sign_in_file(account, token_dir, purpose)
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
    if check_address:
        profile = gmail_service(credentials).users().getProfile(userId="me").execute()
        signed_in_address = str(profile["emailAddress"])
        if signed_in_address.casefold() != account.casefold():
            raise WrongAccountSignedIn(account, signed_in_address)
    _store(credentials, token_file)
    return credentials


def sign_in_file(account: str, token_dir: Path, purpose: str | None = None) -> Path:
    """The file an account's sign-in is stored in, for reading or for another purpose.

    An address is the same address whatever its capitals, so a sign-in stored under
    another spelling of it is found, also where file names tell capitals apart.
    """
    name = f"{account}.{purpose}.json" if purpose else f"{account}.json"
    as_named = token_dir / name
    if as_named.is_file() or not token_dir.is_dir():
        return as_named
    same_address = [p for p in token_dir.glob("*.json") if p.name.casefold() == name.casefold()]
    return same_address[0] if same_address else as_named


def store_sign_in(account: str, credentials: Credentials, token_dir: Path) -> None:
    """Stores a reading sign-in obtained another way, where sign_in will find it."""
    _store(credentials, sign_in_file(account, token_dir))


def forget_sign_in(account: str, token_dir: Path) -> bool:
    """Deletes an account's stored reading sign-in. Says whether there was one."""
    token_file = sign_in_file(account, token_dir)
    if not token_file.is_file():
        return False
    token_file.unlink()
    return True


@dataclass(frozen=True)
class StoredSignIn:
    """What may be known of a stored reading sign-in without opening it for use.

    The fingerprint is a one-way digest of the refresh token: it tells whether the
    sign-in was replaced, and cannot be used to sign in.
    """

    account: str
    scopes: frozenset[str]
    fingerprint: str


def stored_sign_in(account: str, token_dir: Path) -> StoredSignIn | None:
    """The account's stored reading sign-in, if it can be used to read Gmail."""
    return _described(account, sign_in_file(account, token_dir))


def stored_sign_ins(token_dir: Path) -> list[StoredSignIn]:
    """Every stored sign-in in the folder that can read Gmail, by address."""
    if not token_dir.is_dir():
        return []
    found = (_described(path.name.removesuffix(".json"), path) for path in token_dir.glob("*.json"))
    return sorted((each for each in found if each is not None), key=lambda each: each.account)


def _described(account: str, token_file: Path) -> StoredSignIn | None:
    credentials = _stored(token_file, [GMAIL_READONLY])
    if credentials is None:
        return None
    refresh_token = str(credentials.refresh_token)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    scopes = cast(Sequence[str], credentials.scopes or ())  # pyright: ignore[reportUnknownMemberType]
    return StoredSignIn(
        account=account,
        scopes=frozenset(scopes),
        fingerprint=hashlib.sha256(refresh_token.encode()).hexdigest(),
    )


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

    It is written to a new file that is private from the instant it is created, which
    then takes the place of any earlier one. The file is never readable by others, and
    a sign-in is never left half written. The new file has a name nobody can predict and
    is never one that was already there, so nobody can put a file of their own in its
    place beforehand.
    """
    token_file.parent.mkdir(parents=True, exist_ok=True)
    content: str = credentials.to_json()  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    descriptor, name = tempfile.mkstemp(
        dir=token_file.parent, prefix=f"{token_file.name}.", suffix=".part"
    )
    being_written = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(content)
        os.replace(being_written, token_file)
    finally:
        with contextlib.suppress(OSError):
            being_written.unlink()
