"""The guided setup that signs each source account in, checked without a browser."""

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from google.oauth2.credentials import Credentials

from invoice_collector.google_auth import NotSignedIn, SignInExpired, WrongAccountSignedIn
from invoice_collector.setup_accounts import main

GMAIL_READONLY = "https://www.googleapis.com/auth/gmail.readonly"
DRIVE_FILE = "https://www.googleapis.com/auth/drive.file"
FINANCE, OPS, FOUNDER = "finance@acme.test", "ops@acme.test", "founder@acme.test"


class SignIn:
    """Stands in for signing in, remembering what was asked and failing on request."""

    def __init__(self, failing: dict[str, Exception] | None = None) -> None:
        self.asked: list[dict[str, Any]] = []
        self._failing = failing or {}

    def __call__(
        self,
        account: str,
        scopes: Sequence[str],
        token_dir: Path,
        client_file: Path,
        *,
        allow_browser: bool = True,
        renew: bool = False,
    ) -> Credentials:
        self.asked.append(
            {
                "account": account,
                "scopes": list(scopes),
                "token_dir": token_dir,
                "client_file": client_file,
                "allow_browser": allow_browser,
                "renew": renew,
            }
        )
        if account in self._failing and not renew:
            raise self._failing[account]
        return Credentials(token="made-up")  # pyright: ignore[reportUnknownVariableType]


def test_each_source_account_is_signed_in_with_read_only_gmail_access(
    capsys: pytest.CaptureFixture[str],
) -> None:
    sign_in = SignIn()

    status = main([FINANCE, OPS, FOUNDER], sign_in=sign_in)

    assert status == 0
    assert [(a["account"], a["scopes"]) for a in sign_in.asked] == [
        (FINANCE, [GMAIL_READONLY]),
        (OPS, [GMAIL_READONLY]),
        (FOUNDER, [GMAIL_READONLY]),
    ]
    printed = capsys.readouterr().out
    for account in (FINANCE, OPS, FOUNDER):
        assert f"{account}: signed in" in printed


def test_owner_account_is_also_asked_for_access_to_the_files_the_tool_creates() -> None:
    sign_in = SignIn()

    main([FINANCE, OPS, "--owner", OPS], sign_in=sign_in)

    assert [(a["account"], a["scopes"]) for a in sign_in.asked] == [
        (FINANCE, [GMAIL_READONLY]),
        (OPS, [GMAIL_READONLY, DRIVE_FILE]),
    ]


def test_owner_account_is_signed_in_even_when_not_listed_as_a_source_account() -> None:
    sign_in = SignIn()

    main([FINANCE, "--owner", OPS], sign_in=sign_in)

    assert [a["account"] for a in sign_in.asked] == [FINANCE, OPS]
    assert sign_in.asked[1]["scopes"] == [GMAIL_READONLY, DRIVE_FILE]


def test_person_is_told_what_to_expect_in_the_browser(
    capsys: pytest.CaptureFixture[str],
) -> None:
    main([FINANCE], sign_in=SignIn())

    printed = capsys.readouterr().out
    assert "browser" in printed
    assert "unverified" in printed
    assert "7 days" in printed
    assert "read-only" in printed


def test_sign_ins_are_stored_where_asked() -> None:
    sign_in = SignIn()

    main(
        [FINANCE, "--token-dir", "some/tokens", "--client-file", "some/client.json"],
        sign_in=sign_in,
    )

    assert sign_in.asked[0]["token_dir"] == Path("some/tokens")
    assert sign_in.asked[0]["client_file"] == Path("some/client.json")


def test_signing_in_as_the_wrong_address_fails_that_account_and_the_rest_continue(
    capsys: pytest.CaptureFixture[str],
) -> None:
    sign_in = SignIn({FINANCE: WrongAccountSignedIn(FINANCE, "someone.else@acme.test")})

    status = main([FINANCE, OPS], sign_in=sign_in)

    assert status == 1
    printed = capsys.readouterr().out
    assert f"{FINANCE}: NOT signed in" in printed
    assert "someone.else@acme.test" in printed
    assert f"{OPS}: signed in" in printed


def test_expired_sign_in_is_renewed_through_the_browser(
    capsys: pytest.CaptureFixture[str],
) -> None:
    sign_in = SignIn({FINANCE: SignInExpired(FINANCE)})

    status = main([FINANCE], sign_in=sign_in)

    assert status == 0
    assert [a["renew"] for a in sign_in.asked] == [False, True]
    assert f"{FINANCE}: signed in" in capsys.readouterr().out


def test_missing_client_file_is_explained(capsys: pytest.CaptureFixture[str]) -> None:
    sign_in = SignIn({FINANCE: FileNotFoundError("the OAuth desktop client file is missing")})

    status = main([FINANCE], sign_in=sign_in)

    assert status == 1
    assert "client file is missing" in capsys.readouterr().out


def test_owner_named_in_other_capitals_is_the_same_account() -> None:
    sign_in = SignIn()

    status = main(["Ops@acme.test", FINANCE, "--owner", "ops@acme.test"], sign_in=sign_in)

    assert status == 0
    assert [(a["account"], a["scopes"]) for a in sign_in.asked] == [
        ("Ops@acme.test", [GMAIL_READONLY, DRIVE_FILE]),
        (FINANCE, [GMAIL_READONLY]),
    ]


def test_account_named_twice_in_different_capitals_is_signed_in_once() -> None:
    sign_in = SignIn()

    main([FINANCE, "FINANCE@acme.test"], sign_in=sign_in)

    assert [a["account"] for a in sign_in.asked] == [FINANCE]


def test_person_is_told_the_command_to_run_next(capsys: pytest.CaptureFixture[str]) -> None:
    main([FINANCE, OPS], sign_in=SignIn())

    printed = capsys.readouterr().out
    assert f"invoice-collector collect YYYY-MM --account {FINANCE} --account {OPS}" in printed


def test_status_reports_each_read_only_sign_in_without_opening_the_browser(
    capsys: pytest.CaptureFixture[str],
) -> None:
    sign_in = SignIn({FINANCE: NotSignedIn(FINANCE), OPS: SignInExpired(OPS)})

    status = main(["--status", FINANCE, OPS, FOUNDER], sign_in=sign_in)

    assert status == 1
    assert [(a["account"], a["scopes"]) for a in sign_in.asked] == [
        (FINANCE, [GMAIL_READONLY]),
        (OPS, [GMAIL_READONLY]),
        (FOUNDER, [GMAIL_READONLY]),
    ]
    assert not any(a["allow_browser"] or a["renew"] for a in sign_in.asked)
    printed = capsys.readouterr().out
    assert f"{FINANCE}: missing" in printed
    assert f"{OPS}: expired" in printed
    assert f"{FOUNDER}: present" in printed
    assert "unverified" not in printed
    assert f"invoice-collector-setup {FINANCE} {OPS}" in printed


def test_status_when_every_sign_in_is_present_succeeds(
    capsys: pytest.CaptureFixture[str],
) -> None:
    status = main(["--status", FINANCE], sign_in=SignIn())

    assert status == 0
    assert f"{FINANCE}: present" in capsys.readouterr().out
