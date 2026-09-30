"""The sample mail, put into a test mailbox from the Source accounts screen.

A reviewer with nothing but Docker has no command line to seed her mailboxes with, so an
administrator does it here, for a connected source account: the sample emails of one sample
mailbox are inserted into it, addressed to it, with their portal links written for the
sample portal the runner can reach. Inserting needs leave to insert mail, which is asked of
Google through the web sign-in and stored apart from the reading sign-in, as the seed
command stores it, so what the collection may do is never widened. Running it again inserts
nothing twice, since each message is looked up first with the reading sign-in.

The expected vendors that bill the sample mailbox can be added to the expected vendor list
at the same time, as billed to the real address, when the person chooses it.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

from google.auth.exceptions import GoogleAuthError
from googleapiclient.errors import Error as GoogleApiError

from invoice_collector import google_auth
from invoice_collector.domain import ExpectedVendor
from invoice_collector.gap_report import ExpectedVendorFileInvalid, read_expected_vendors
from invoice_collector.google_auth import GMAIL_INSERT, GMAIL_READONLY, SEEDING, GmailService
from invoice_collector.seed.addressing import addressed_to
from invoice_collector.seed.generator import SeedMessage, load_messages
from invoice_collector.seed.gmail_insert import InsertedElsewhere, InsertReport, insert_messages

GOLDEN_FILE = "golden.json"
EXPECTED_VENDORS_FILE = "expected_vendors.json"


class SampleMailUnavailable(Exception):
    """There is no sample mail to put into a mailbox. The message says why."""


class SampleMailNotInserted(Exception):
    """The sample mail could not be put into the mailbox. The message says why."""


@dataclass(frozen=True)
class SampleMailbox:
    """One mailbox of the sample mail: its emails, and the expected vendors that bill it."""

    name: str
    emails: int
    vendors: tuple[str, ...]


class SampleMail:
    """The sample mail in a folder, as `invoice-collector-seed generate` writes it."""

    def __init__(self, folder: Path, portal_url: str | None = None) -> None:
        self._folder = folder
        # Where the runner reaches the sample portal. Portal links are left as generated
        # without one, and a run then refuses them, as it refuses any local link.
        self._portal_url = portal_url

    @property
    def portal_url(self) -> str | None:
        return self._portal_url

    def _golden(self) -> list[dict[str, Any]]:
        try:
            loaded: Any = json.loads((self._folder / GOLDEN_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise SampleMailUnavailable(
                f"There is no sample mail in {self._folder}: it holds no readable {GOLDEN_FILE}."
            ) from None
        if not isinstance(loaded, list):
            raise SampleMailUnavailable(f"{self._folder / GOLDEN_FILE} is not a list of emails.")
        return [entry for entry in loaded if isinstance(entry, dict)]  # pyright: ignore[reportUnknownVariableType]

    def _expected_vendors(self) -> list[ExpectedVendor]:
        path = self._folder / EXPECTED_VENDORS_FILE
        if not path.is_file():
            return []
        try:
            return read_expected_vendors(path)
        except ExpectedVendorFileInvalid as problem:
            raise SampleMailUnavailable(str(problem)) from None

    def mailboxes(self) -> list[SampleMailbox]:
        """Each sample mailbox, with how many emails it holds and who bills it."""
        counts: dict[str, int] = {}
        for entry in self._golden():
            account = entry.get("source_account")
            if isinstance(account, str) and account:
                counts[account] = counts.get(account, 0) + 1
        vendors = self._expected_vendors()
        return [
            SampleMailbox(
                name=name,
                emails=count,
                vendors=tuple(v.vendor for v in vendors if v.source_account == name),
            )
            for name, count in sorted(counts.items())
        ]

    def _mailbox(self, name: str) -> SampleMailbox:
        for mailbox in self.mailboxes():
            if mailbox.name == name:
                return mailbox
        raise SampleMailUnavailable(f"{name} is not a sample mailbox.")

    def messages_for(self, name: str, address: str) -> list[SeedMessage]:
        """The emails of the sample mailbox, as received at address."""
        self._mailbox(name)
        return [
            addressed_to(message, address, self._portal_url)
            for message in load_messages(self._folder, name)
        ]

    def vendors_for(self, name: str, address: str) -> list[ExpectedVendor]:
        """The expected vendors that bill the sample mailbox, as billing address instead."""
        self._mailbox(name)
        return [
            replace(vendor, source_account=address, status="expected")
            for vendor in self._expected_vendors()
            if vendor.source_account == name
        ]


class SampleMailInserter(Protocol):
    def can_insert(self, address: str) -> bool:
        """Whether leave to insert mail into the mailbox is stored and still works."""
        ...

    def insert(self, address: str, messages: Sequence[SeedMessage]) -> InsertReport:
        """Inserts each message the mailbox does not hold yet.

        Raises SampleMailNotInserted, saying why, when it cannot.
        """
        ...


class GmailSampleMailInserter:
    """Inserts through the Gmail API with the stored sign-in that may insert mail.

    Messages are looked up with the reading sign-in, which the insert scope cannot do, and
    the first message inserted is looked for through it, so leave to insert that belongs to
    another address stops at once.
    """

    def __init__(
        self, token_dir: Path, gmail_service: GmailService = google_auth.gmail_service
    ) -> None:
        self._token_dir = token_dir
        self._gmail_service = gmail_service

    def _inserting(self, address: str) -> Any:
        return google_auth.sign_in(
            address,
            [GMAIL_INSERT],
            self._token_dir,
            allow_browser=False,
            purpose=SEEDING,
            check_address=False,
        )

    def can_insert(self, address: str) -> bool:
        try:
            self._inserting(address)
        except (google_auth.SignInExpired, OSError, ValueError, GoogleAuthError):
            return False
        return True

    def insert(self, address: str, messages: Sequence[SeedMessage]) -> InsertReport:
        try:
            reading = google_auth.sign_in(
                address, [GMAIL_READONLY], self._token_dir, allow_browser=False
            )
            inserting = self._inserting(address)
            return insert_messages(
                self._gmail_service(inserting),
                messages,
                reader=self._gmail_service(reading),
            )
        except google_auth.SignInExpired as problem:
            raise SampleMailNotInserted(
                f"The sign-in for {address} does not work ({problem}). Renew it, then try again."
            ) from None
        except InsertedElsewhere as problem:
            raise SampleMailNotInserted(str(problem)) from None
        except (GoogleApiError, GoogleAuthError, OSError, ValueError):
            # The error itself may quote Google's answer, so it is not shown.
            raise SampleMailNotInserted(
                f"Gmail refused or could not be reached while inserting into {address}. What "
                "was inserted stays; try again to insert the rest."
            ) from None


@dataclass
class FakeSampleMailInserter:
    """Stands in for Gmail in tests, remembering what each mailbox holds."""

    allowed: set[str] = field(default_factory=set[str])
    held: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    failing: str | None = None

    def can_insert(self, address: str) -> bool:
        return address in self.allowed

    def insert(self, address: str, messages: Sequence[SeedMessage]) -> InsertReport:
        if self.failing is not None:
            raise SampleMailNotInserted(self.failing)
        mailbox = self.held.setdefault(address, [])
        inserted = [m.message_id for m in messages if m.message_id not in mailbox]
        mailbox += inserted
        skipped = [m.message_id for m in messages if m.message_id not in inserted]
        return InsertReport(tuple(inserted), tuple(skipped))
