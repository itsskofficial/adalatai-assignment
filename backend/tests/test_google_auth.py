"""Sign-in for a source account, checked without calling Google.

Refreshes go to a local server replaying recorded responses, and the browser is
replaced by a function that hands back a sign-in.
"""

import json
import stat
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import pytest
from conftest import ReplayServer
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build  # pyright: ignore[reportUnknownVariableType]
from googleapiclient.http import HttpMockSequence

from invoice_collector.google_auth import (
    GMAIL_READONLY,
    GmailService,
    NotSignedIn,
    SignInExpired,
    WrongAccountSignedIn,
    sign_in,
)

RECORDED = Path(__file__).parent / "recorded"
REVOKED = json.loads((RECORDED / "google_sign_in_revoked.json").read_text("utf-8"))
REFRESHED = json.loads((RECORDED / "google_sign_in_refreshed.json").read_text("utf-8"))
PROFILE = (RECORDED / "gmail_profile.json").read_text("utf-8")
ACCOUNT = "finance@acme.test"
CLIENT_FILE = Path("no-such-folder/desktop-client.json")
DRIVE_FILE = "https://www.googleapis.com/auth/drive.file"
LONG_AGO, FAR_AHEAD = "2020-01-01T00:00:00Z", "2999-01-01T00:00:00Z"


def stored_sign_in(
    token_dir: Path,
    *,
    expiry: str = FAR_AHEAD,
    token_uri: str = "http://127.0.0.1:9/never-called",
    scopes: Sequence[str] = (GMAIL_READONLY,),
) -> Path:
    path = token_dir / f"{ACCOUNT}.json"
    path.write_text(
        json.dumps(
            {
                "token": "stored-access-value",
                "refresh_token": "stored-refresh-value",
                "token_uri": token_uri,
                "client_id": "made-up-client.apps.googleusercontent.com",
                "client_secret": "made-up",
                "scopes": list(scopes),
                "expiry": expiry,
            }
        ),
        encoding="utf-8",
    )
    return path


class Browser:
    """Stands in for the person signing in through the browser."""

    def __init__(self) -> None:
        self.asked_for: list[list[str]] = []

    def __call__(self, client_file: Path, scopes: Sequence[str]) -> Credentials:
        self.asked_for.append(list(scopes))
        return Credentials(  # pyright: ignore[reportUnknownVariableType]
            token="browser-access-value",
            refresh_token="browser-refresh-value",
            token_uri="http://127.0.0.1:9/never-called",
            client_id="made-up-client.apps.googleusercontent.com",
            client_secret="made-up",
            scopes=list(scopes),
        )


def gmail_answering(profile: str) -> GmailService:
    def service(credentials: Credentials) -> Any:
        return cast(
            Any, build("gmail", "v1", http=HttpMockSequence([({"status": "200"}, profile)]))
        )

    return service


def access_value(credentials: Credentials) -> str:
    return str(cast(Any, credentials.token))  # pyright: ignore[reportUnknownMemberType]


def test_stored_sign_in_is_reused_without_opening_the_browser(tmp_path: Path) -> None:
    stored_sign_in(tmp_path)
    browser = Browser()

    credentials = sign_in(ACCOUNT, [GMAIL_READONLY], tmp_path, CLIENT_FILE, browser_flow=browser)

    assert access_value(credentials) == "stored-access-value"
    assert browser.asked_for == []


def test_stored_sign_in_that_has_run_out_is_refreshed_and_stored(
    tmp_path: Path, replay_server: ReplayServer
) -> None:
    path = stored_sign_in(tmp_path, expiry=LONG_AGO, token_uri=replay_server(200, REFRESHED))

    credentials = sign_in(ACCOUNT, [GMAIL_READONLY], tmp_path, CLIENT_FILE, browser_flow=Browser())

    assert access_value(credentials) == REFRESHED["access_token"]
    assert json.loads(path.read_text("utf-8"))["token"] == REFRESHED["access_token"]


def test_expired_sign_in_is_reported_naming_the_source_account(
    tmp_path: Path, replay_server: ReplayServer
) -> None:
    stored_sign_in(tmp_path, expiry=LONG_AGO, token_uri=replay_server(400, REVOKED))
    browser = Browser()

    with pytest.raises(SignInExpired, match=ACCOUNT) as raised:
        sign_in(ACCOUNT, [GMAIL_READONLY], tmp_path, CLIENT_FILE, browser_flow=browser)

    assert raised.value.source_account == ACCOUNT
    assert browser.asked_for == []


def test_first_sign_in_goes_through_the_browser_and_is_stored(tmp_path: Path) -> None:
    browser = Browser()
    token_dir = tmp_path / "tokens"

    credentials = sign_in(
        ACCOUNT,
        [GMAIL_READONLY],
        token_dir,
        CLIENT_FILE,
        browser_flow=browser,
        gmail_service=gmail_answering(PROFILE),
    )

    assert access_value(credentials) == "browser-access-value"
    assert browser.asked_for == [[GMAIL_READONLY]]
    stored = json.loads((token_dir / f"{ACCOUNT}.json").read_text("utf-8"))
    assert stored["refresh_token"] == "browser-refresh-value"


def test_signing_in_as_a_different_address_is_rejected_and_not_stored(tmp_path: Path) -> None:
    profile = json.dumps({**json.loads(PROFILE), "emailAddress": "someone.else@acme.test"})

    with pytest.raises(WrongAccountSignedIn, match="someone.else@acme.test") as raised:
        sign_in(
            ACCOUNT,
            [GMAIL_READONLY],
            tmp_path,
            CLIENT_FILE,
            browser_flow=Browser(),
            gmail_service=gmail_answering(profile),
        )

    assert ACCOUNT in str(raised.value)
    assert list(tmp_path.iterdir()) == []


def test_address_is_matched_whatever_its_letter_case(tmp_path: Path) -> None:
    sign_in(
        "Finance@Acme.test",
        [GMAIL_READONLY],
        tmp_path,
        CLIENT_FILE,
        browser_flow=Browser(),
        gmail_service=gmail_answering(PROFILE),
    )

    assert [p.name for p in tmp_path.iterdir()] == ["Finance@Acme.test.json"]


def test_stored_sign_in_lacking_a_requested_scope_goes_through_the_browser(tmp_path: Path) -> None:
    stored_sign_in(tmp_path)
    browser = Browser()

    sign_in(
        ACCOUNT,
        [GMAIL_READONLY, DRIVE_FILE],
        tmp_path,
        CLIENT_FILE,
        browser_flow=browser,
        gmail_service=gmail_answering(PROFILE),
    )

    assert browser.asked_for == [[GMAIL_READONLY, DRIVE_FILE]]


def test_renewing_goes_through_the_browser_even_with_a_stored_sign_in(tmp_path: Path) -> None:
    stored_sign_in(tmp_path)
    browser = Browser()

    credentials = sign_in(
        ACCOUNT,
        [GMAIL_READONLY],
        tmp_path,
        CLIENT_FILE,
        renew=True,
        browser_flow=browser,
        gmail_service=gmail_answering(PROFILE),
    )

    assert access_value(credentials) == "browser-access-value"


def test_source_account_never_signed_in_is_reported_when_the_browser_is_not_allowed(
    tmp_path: Path,
) -> None:
    browser = Browser()

    with pytest.raises(NotSignedIn, match=ACCOUNT):
        sign_in(
            ACCOUNT,
            [GMAIL_READONLY],
            tmp_path,
            CLIENT_FILE,
            allow_browser=False,
            browser_flow=browser,
        )

    assert browser.asked_for == []


def sign_in_through_the_browser(token_dir: Path) -> Path:
    sign_in(
        ACCOUNT,
        [GMAIL_READONLY],
        token_dir,
        CLIENT_FILE,
        renew=True,
        browser_flow=Browser(),
        gmail_service=gmail_answering(PROFILE),
    )
    return token_dir / f"{ACCOUNT}.json"


@pytest.mark.skipif(sys.platform == "win32", reason="Windows does not keep these permissions")
def test_stored_sign_in_can_be_read_by_its_owner_alone(tmp_path: Path) -> None:
    stored = sign_in_through_the_browser(tmp_path / "tokens")

    assert stat.S_IMODE(stored.stat().st_mode) == 0o600


@pytest.mark.skipif(sys.platform == "win32", reason="Windows does not keep these permissions")
def test_sign_in_stored_over_one_that_others_could_read_is_private(tmp_path: Path) -> None:
    token_dir = tmp_path / "tokens"
    token_dir.mkdir()
    readable_by_all = token_dir / f"{ACCOUNT}.json"
    readable_by_all.write_text("{}", encoding="utf-8")
    readable_by_all.chmod(0o644)

    stored = sign_in_through_the_browser(token_dir)

    assert stat.S_IMODE(stored.stat().st_mode) == 0o600
    assert json.loads(stored.read_text("utf-8"))["refresh_token"] == "browser-refresh-value"


def test_storing_a_sign_in_leaves_nothing_else_behind(tmp_path: Path) -> None:
    token_dir = tmp_path / "tokens"

    sign_in_through_the_browser(token_dir)
    sign_in_through_the_browser(token_dir)

    assert [p.name for p in token_dir.iterdir()] == [f"{ACCOUNT}.json"]
