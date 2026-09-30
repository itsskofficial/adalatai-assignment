"""Where the dashboard files an approved or uploaded billing document.

To the owner account's Drive first, as a run with an owner account files one, and to the
local archive beside the ledger. The owner account is looked up each time a document is
filed (owner_account.py), so choosing it or renewing its sign-in on the Source accounts
screen counts at once. When there is one whose Drive cannot be reached because its sign-in
does not work, the document is filed locally only, as with no owner account, and the person
is told so.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from google.oauth2.credentials import Credentials

from invoice_collector.archive import Archive, BothArchives
from invoice_collector.collection_settings import SettingsStore
from invoice_collector.drive_archive import DriveArchiveInChosenFolder
from invoice_collector.owner_account import (
    DriveNotReached,
    OwnerState,
    drive_credentials,
    owner_account,
    owner_state,
)

GoogleServices = Callable[[Credentials], tuple[Any, Any]]


class OwnerDrive(Protocol):
    def state(self) -> OwnerState | None:
        """The owner account and whether its Drive can be used; None with no owner account."""
        ...

    def archive(self) -> Archive | None:
        """The owner account's Drive, None with no owner account. Raises DriveNotReached when
        there is one whose Drive cannot be used just now."""
        ...


class GoogleOwnerDrive:
    """The owner account's Google Drive, filing under the folder the settings name."""

    def __init__(
        self,
        ledger_path: Path,
        token_dir: Path,
        named: str | None,
        google_services: GoogleServices,
    ) -> None:
        self._ledger_path = ledger_path
        self._token_dir = token_dir
        self._named = named
        self._google_services = google_services
        self._settings = SettingsStore(ledger_path)

    def state(self) -> OwnerState | None:
        return owner_state(self._ledger_path, self._named, self._token_dir)

    def archive(self) -> Archive | None:
        owner = owner_account(self._ledger_path, self._named)
        if owner is None:
            return None
        drive, _ = self._google_services(drive_credentials(owner, self._token_dir))
        return DriveArchiveInChosenFolder(drive, lambda: self._settings.read().drive_folder)


class GivenDrive:
    """A Drive archive handed in, as tests hand in a fake: always reached, naming nobody."""

    def __init__(self, archive: Archive) -> None:
        self._archive = archive

    def state(self) -> OwnerState | None:
        return None

    def archive(self) -> Archive | None:
        return self._archive


@dataclass(frozen=True)
class Filing:
    """Where a document is filed just now."""

    archive: Archive
    reaches_drive: bool
    # Why the owner account's Drive was not reached, when there is an owner account.
    drive_not_reached: str | None = None

    def warnings(self) -> list[str]:
        if self.drive_not_reached is None:
            return []
        return [
            f"Google Drive was not reached: {self.drive_not_reached}. The billing document "
            "was filed on this machine only, and a later run does not copy it to Drive."
        ]


def filing(owner_drive: OwnerDrive | None, local: Archive) -> Filing:
    """The owner account's Drive and the local archive, or the local archive alone."""
    if owner_drive is None:
        return Filing(local, reaches_drive=False)
    try:
        drive = owner_drive.archive()
    except DriveNotReached as not_reached:
        return Filing(local, reaches_drive=False, drive_not_reached=str(not_reached))
    if drive is None:
        return Filing(local, reaches_drive=False)
    return Filing(BothArchives(drive, local), reaches_drive=True)
