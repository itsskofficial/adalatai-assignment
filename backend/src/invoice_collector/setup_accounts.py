"""Guided setup: signs each source account in through the browser.

invoice-collector-setup finance@example.com ops@example.com --owner ops@example.com
"""

import argparse
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from google.auth.exceptions import GoogleAuthError
from google.oauth2.credentials import Credentials
from googleapiclient.errors import Error as GoogleApiError

from invoice_collector import google_auth
from invoice_collector.google_auth import (
    DEFAULT_CLIENT_FILE,
    DEFAULT_TOKEN_DIR,
    DRIVE_FILE,
    GMAIL_READONLY,
    SignInExpired,
    WrongAccountSignedIn,
)

INTRODUCTION = """\
This signs each account in to Google so the tool can read its mail.

For each account, a browser window opens. In it:
  1. Choose the account named here. Signing in as any other address is rejected.
  2. Google warns that the app is unverified ("Google hasn't verified this app").
     That is expected while the OAuth app is in testing: choose "Advanced", then
     "Go to ... (unsafe)", or "Continue".
  3. Tick the access asked for and allow it. Source accounts are asked for read-only
     access to Gmail and nothing more. The owner account is also asked for access to
     the Drive files this tool itself creates.
  4. Come back here once the browser says the sign-in is complete.

Sign-ins are stored on this machine in {token_dir}, which git ignores.
While the OAuth app is in testing, a sign-in expires after 7 days: run this again
when a run reports that an account's sign-in no longer works.
"""


class SignIn(Protocol):
    def __call__(
        self,
        account: str,
        scopes: Sequence[str],
        token_dir: Path,
        client_file: Path,
        *,
        renew: bool = False,
    ) -> Credentials: ...


def main(argv: Sequence[str] | None = None, sign_in: SignIn = google_auth.sign_in) -> int:
    parser = argparse.ArgumentParser(
        prog="invoice-collector-setup",
        description="Sign each source account in to Google and store its sign-in.",
    )
    parser.add_argument(
        "accounts", nargs="*", metavar="ACCOUNT", help="address of a source account"
    )
    parser.add_argument(
        "--owner",
        metavar="ACCOUNT",
        help="the owner account, which is also given access to the Drive files the tool creates",
    )
    parser.add_argument("--token-dir", type=Path, default=DEFAULT_TOKEN_DIR)
    parser.add_argument("--client-file", type=Path, default=DEFAULT_CLIENT_FILE)
    args = parser.parse_args(argv)

    accounts: list[str] = list(dict.fromkeys(args.accounts))
    owner: str | None = args.owner
    if owner is not None and owner not in accounts:
        accounts.append(owner)
    if not accounts:
        parser.error("name at least one account")

    print(INTRODUCTION.format(token_dir=args.token_dir))
    failed: list[str] = []
    for number, account in enumerate(accounts, start=1):
        scopes = [GMAIL_READONLY, DRIVE_FILE] if account == owner else [GMAIL_READONLY]
        asked_for = "read-only Gmail" + (
            " and the tool's own Drive files" if account == owner else ""
        )
        print(f"[{number}/{len(accounts)}] {account}: asking for access to {asked_for}.")
        print(f"    If a browser window opens, sign in as {account}.")
        try:
            try:
                sign_in(account, scopes, args.token_dir, args.client_file)
            except SignInExpired:
                print("    The stored sign-in no longer works. Opening the browser to renew it.")
                sign_in(account, scopes, args.token_dir, args.client_file, renew=True)
        except (
            SignInExpired,
            WrongAccountSignedIn,
            OSError,
            ValueError,
            GoogleAuthError,
            GoogleApiError,
        ) as error:
            failed.append(account)
            print(f"    {account}: NOT signed in. {error}")
        else:
            print(f"    {account}: signed in, and the sign-in is stored.")

    print()
    if failed:
        print(f"Not signed in: {', '.join(failed)}. Run this again for those accounts.")
        return 1
    print(f"All {len(accounts)} accounts are signed in.")
    return 0
