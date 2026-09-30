"""Which source account is the owner account, and whether its sign-in reaches Drive.

The owner account is chosen on the Source accounts screen, which records it in the source
account registry in the ledger. INVOICE_COLLECTOR_GOOGLE_OWNER, or the --google-owner option
of the dashboard and the runner, names it for a setup with no dashboard, and is used only
while the screen has chosen none. When the screen has chosen another, the screen's choice is
used, and the address the setting names is said to be set aside.

The owner account is looked up each time it is needed, so a choice or a renewed sign-in on
the screen counts at once, without restarting anything. Neither the dashboard nor the runner
refuses to start over it: the screen that connects it is the dashboard's.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from google.auth.exceptions import GoogleAuthError
from google.oauth2.credentials import Credentials

from invoice_collector import drive_archive, google_auth
from invoice_collector.source_account_registry import (
    SourceAccountRegistry,
    normalise,
    owner_account_in,
)

# The owner account, for a setup with no dashboard, when the commands are not given
# --google-owner. The owner account chosen on the Source accounts screen wins over it.
GOOGLE_OWNER_VARIABLE = "INVOICE_COLLECTOR_GOOGLE_OWNER"

ChosenOn = Literal["source_accounts_screen", "setting"]


class DriveNotReached(Exception):
    """The owner account's Drive cannot be used just now. The message says why."""


@dataclass(frozen=True)
class OwnerAccount:
    address: str
    # Chosen on the Source accounts screen, or named by the setting or the option.
    chosen_on: ChosenOn
    # The address the setting or the option names, when the screen chose another.
    set_aside: str | None = None

    def set_aside_message(self) -> str | None:
        if self.set_aside is None:
            return None
        return (
            f"{GOOGLE_OWNER_VARIABLE} (or --google-owner) names {self.set_aside}, but "
            f"{self.address} is the owner account chosen on the Source accounts screen, which "
            "is used. Unset the setting, or make that account the owner on the screen."
        )


@dataclass(frozen=True)
class OwnerState:
    """The owner account, and whether its Drive can be used just now."""

    owner: OwnerAccount
    # Whether it is a connected source account.
    connected: bool
    # Why its Drive cannot be used, in plain words; None when it can.
    problem: str | None


def owner_account(ledger_path: Path, named: str | None) -> OwnerAccount | None:
    """The owner account: the one chosen on the Source accounts screen, or else the one named
    by the setting or the option. None when there is neither."""
    chosen = owner_account_in(ledger_path)
    named = normalise(named) if named and named.strip() else None
    if chosen is not None:
        set_aside = named if named is not None and named != chosen else None
        return OwnerAccount(chosen, "source_accounts_screen", set_aside)
    if named is not None:
        return OwnerAccount(named, "setting")
    return None


def not_signed_in(owner: OwnerAccount) -> str:
    """What to say when the owner account's sign-in does not reach Drive."""
    mend = (
        "Renew its sign-in on the Source accounts screen"
        if owner.chosen_on == "source_accounts_screen"
        else "Connect it on the Source accounts screen as the owner account, or renew its "
        "sign-in there"
    )
    return (
        f"The owner account {owner.address} is not signed in to Google Drive, or its sign-in "
        f"no longer works. {mend}, or sign it in with: invoice-collector-setup --owner "
        f"{owner.address}"
    )


def drive_credentials(owner: OwnerAccount, token_dir: Path) -> Credentials:
    """The owner account's stored sign-in for Drive. Never opens a browser.

    Raises DriveNotReached when there is none, it no longer works, or Google cannot be asked.
    """
    try:
        return google_auth.sign_in(
            owner.address, drive_archive.SCOPES, token_dir, allow_browser=False
        )
    except google_auth.SignInExpired:
        raise DriveNotReached(not_signed_in(owner)) from None
    except (OSError, ValueError, GoogleAuthError):
        # The error itself is not repeated: it may quote Google's answer.
        raise DriveNotReached(
            f"The sign-in of the owner account {owner.address} for Google Drive could not be "
            "checked: Google could not be reached"
        ) from None


def owner_state(ledger_path: Path, named: str | None, token_dir: Path) -> OwnerState | None:
    """The owner account and whether its Drive can be used, or None with no owner account."""
    owner = owner_account(ledger_path, named)
    if owner is None:
        return None
    problem: str | None = None
    try:
        drive_credentials(owner, token_dir)
    except DriveNotReached as not_reached:
        problem = str(not_reached)
    connected = (
        ledger_path.is_file() and SourceAccountRegistry(ledger_path).find(owner.address) is not None
    )
    return OwnerState(owner, connected, problem)


def said_at_start(state: OwnerState | None, what_follows: str) -> list[str]:
    """What a service says of the owner account when it starts, one warning a line."""
    if state is None:
        return []
    said: list[str] = []
    set_aside = state.owner.set_aside_message()
    if set_aside is not None:
        said.append(f"Warning: {set_aside}")
    if state.problem is not None:
        said.append(f"Warning: {state.problem}. {what_follows}")
    return said
