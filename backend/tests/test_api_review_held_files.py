"""The Review screen finds the PDF of a held document on this machine without guessing, and a
decision stands whatever the tidying up after it meets.

A document filed to Drive as well is linked to Drive, so its local copy is found by name.
Two held documents can have the same vendor, date, total and currency, and so the same
name, and different PDFs: an invoice sent again with its PDF made again. The run gives the
second a numbered name. Each held document must still be filed with its own PDF, whether
it came attached, in the body of an email, or behind a portal link.
"""

import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path, PurePosixPath

import pytest
from support import ENGINEERING, Collection, invoice_email, usd
from test_api import sign_in
from test_api_review import (
    NOW,
    REVIEW,
    FakeDriveArchive,
    action_path,
    approve,
    corrections,
    open_dashboard,
    pending_folder,
    queue,
    state_of,
)

from invoice_collector.archive import BothArchives, LocalArchive, is_named
from invoice_collector.domain import Email, EmailState
from invoice_collector.exchange_rates import FakeExchangeRates

FIGMA = replace(
    usd("Figma", date(2026, 8, 21), "190.00"), confidence="low", doubts="the total is faint"
)
NAME = "2026-08_Figma_190.00-USD.pdf"
NAME_2 = "2026-08_Figma_190.00-USD_2.pdf"


@pytest.fixture
def rates() -> FakeExchangeRates:
    return FakeExchangeRates({"USD": Decimal("95.34")})


@pytest.fixture
def clock() -> list[datetime]:
    return [NOW]


class NumberingDrive(FakeDriveArchive):
    """The owner account's Drive as the real one behaves: a different document saved under
    a name already taken gets a numbered name, and removing finds a copy by its bytes under
    the name or a numbered form of it."""

    def save(self, folder: str, filename: str, pdf: bytes) -> str:
        if not self.reachable:
            raise ConnectionError("Drive could not be reached")
        name = PurePosixPath(filename)
        candidate, number = filename, 2
        while (folder, candidate) in self.files:
            if self.files[(folder, candidate)] == pdf:
                return f"https://drive.example/{folder}/{candidate}"
            candidate = f"{name.stem}_{number}{name.suffix}"
            number += 1
        self.saved.append((folder, candidate, pdf))
        self.files[(folder, candidate)] = pdf
        return f"https://drive.example/{folder}/{candidate}"

    def remove(self, folder: str, filename: str, pdf: bytes) -> None:
        if not (self.reachable and self.removable):
            raise ConnectionError("Drive could not be reached")
        for (where, name), content in list(self.files.items()):
            if where == folder and is_named(name, filename) and content == pdf:
                del self.files[(where, name)]
                return


class DriveThatCannotBeFound(NumberingDrive):
    """Saves, but removing raises an error of a kind the Drive client raises when the
    folder cannot be found, which is neither an OSError nor an HTTP error."""

    def remove(self, folder: str, filename: str, pdf: bytes) -> None:
        raise LookupError("the folder Invoice Collection was not found in Drive")


class DriveLinkingById(NumberingDrive):
    """Links to each file as the real Drive does: by its id, the link naming no file."""

    def __init__(self) -> None:
        super().__init__()
        self.links: dict[tuple[str, str], str] = {}

    def save(self, folder: str, filename: str, pdf: bytes) -> str:
        where = super().save(folder, filename, pdf).removeprefix("https://drive.example/")
        folder_of, _, name = where.rpartition("/")
        key = (folder_of, name)
        if key not in self.links:
            self.links[key] = (
                f"https://drive.google.com/file/d/{len(self.links) + 1}/view?usp=drivesdk"
            )
        return self.links[key]


@dataclass(frozen=True)
class Resent:
    """One Figma invoice sent twice, its PDF made again each time: the same fields, other
    bytes."""

    emails: tuple[Email, Email]
    # Bytes found in the PDF made from each email, and in no other.
    marks: tuple[bytes, bytes]

    def whose(self, pdf: bytes) -> tuple[Email, Email]:
        """The email this PDF was made from, then the other one."""
        [found] = [i for i, mark in enumerate(self.marks) if mark in pdf]
        return self.emails[found], self.emails[1 - found]


def august(day: int) -> datetime:
    return datetime(2026, 8, day, 9, 0, tzinfo=UTC)


def figma_email(message_id: str, day: int, html_body: str) -> Email:
    return Email(
        source_account=ENGINEERING,
        message_id=message_id,
        sender="Figma <billing@figma.example>",
        subject="Your Figma invoice is ready",
        received_at=august(day),
        html_body=html_body,
    )


def attached(collection: Collection) -> Resent:
    pdfs = (b"%PDF-1.7 figma invoice, first send", b"%PDF-1.7 figma invoice, second send")
    for pdf in pdfs:
        collection.answers[pdf] = FIGMA
    emails = (
        invoice_email("Figma", pdfs[0], received=august(21)),
        invoice_email("Figma", pdfs[1], received=august(22)),
    )
    return Resent(emails, pdfs)


def in_the_body(collection: Collection) -> Resent:
    """Each body is rendered to a PDF. The renderer never gives the same bytes twice."""
    bodies = tuple(
        f"<h2>Figma invoice INV-2291</h2><p>Total due $190.00</p><p>Sent {day} August</p>"
        for day in (21, 22)
    )
    for body in bodies:
        for rendering in range(1, 5):
            collection.answers[f"%PDF-rendered {rendering} {body}".encode()] = FIGMA
    emails = (
        figma_email("m-figma-body-21", 21, bodies[0]),
        figma_email("m-figma-body-22", 22, bodies[1]),
    )
    return Resent(emails, (bodies[0].encode(), bodies[1].encode()))


def behind_a_portal_link(collection: Collection) -> Resent:
    links = (
        "https://billing.figma.example/i/in_21",
        "https://billing.figma.example/i/in_22",
    )
    pdfs = (b"%PDF-1.7 figma portal page, first", b"%PDF-1.7 figma portal page, second")
    for link, pdf in zip(links, pdfs, strict=True):
        collection.pages[link] = pdf
        collection.answers[pdf] = FIGMA
    emails = tuple(
        figma_email(
            f"m-figma-link-{day}",
            day,
            f'<p>Your latest invoice is ready.</p><a href="{link}">View invoice</a>',
        )
        for day, link in zip((21, 22), links, strict=True)
    )
    return Resent((emails[0], emails[1]), pdfs)


FORMATS = pytest.mark.parametrize(
    "resend", [attached, in_the_body, behind_a_portal_link], ids=["attached", "body", "portal"]
)


def archive_folder(collection: Collection) -> Path:
    return collection.tmp_path / "archive" / "2026-08"


def hold_in_drive(collection: Collection, drive: NumberingDrive, emails: list[Email]) -> None:
    """Runs August as a run with an owner account does, filing to Drive and locally."""
    collection.archive = BothArchives(drive, LocalArchive(collection.tmp_path / "archive"))
    result = collection.run(emails)
    assert len(result.pending) == len(emails)


def forget_saved_pdfs(collection: Collection) -> None:
    """Makes the ledger one written before the saved PDF of a held document was recorded."""
    with closing(sqlite3.connect(collection.tmp_path / "ledger.sqlite")) as db, db:
        db.execute("UPDATE pending_documents SET pdf_sha256 = NULL")


# Finding the held document's own PDF


@FORMATS
def test_approving_the_second_of_two_alike_documents_files_its_own_pdf(
    collection: Collection,
    rates: FakeExchangeRates,
    clock: list[datetime],
    resend: Callable[[Collection], Resent],
) -> None:
    resent = resend(collection)
    drive = NumberingDrive()
    hold_in_drive(collection, drive, list(resent.emails))
    pending = archive_folder(collection) / "pending"
    assert pending_folder(collection) == [NAME, NAME_2]
    first_pdf, second_pdf = (pending / NAME).read_bytes(), (pending / NAME_2).read_bytes()
    second, first = resent.whose(second_pdf)

    with open_dashboard(collection.tmp_path, rates, clock, drive) as dashboard:
        sign_in(dashboard)

        response = approve(dashboard, second)

        assert response.status_code == 200
        assert (archive_folder(collection) / NAME).read_bytes() == second_pdf
        assert drive.files[("2026-08", NAME)] == second_pdf
        # The first is still held, with its own pending copy in place.
        assert state_of(collection, first)[0] is EmailState.NEEDS_REVIEW
        assert [item["message_id"] for item in queue(dashboard)] == [first.message_id]
        assert pending_folder(collection) == [NAME]
        assert (pending / NAME).read_bytes() == first_pdf
        assert drive.files[("2026-08/pending", NAME)] == first_pdf

        # Approving the first then files its own PDF beside the second.
        assert approve(dashboard, first).status_code == 200
        assert (archive_folder(collection) / NAME_2).read_bytes() == first_pdf
        assert pending_folder(collection) == []


@FORMATS
def test_approving_the_first_of_two_alike_documents_files_its_own_pdf(
    collection: Collection,
    rates: FakeExchangeRates,
    clock: list[datetime],
    resend: Callable[[Collection], Resent],
) -> None:
    resent = resend(collection)
    drive = NumberingDrive()
    hold_in_drive(collection, drive, list(resent.emails))
    pending = archive_folder(collection) / "pending"
    first_pdf, second_pdf = (pending / NAME).read_bytes(), (pending / NAME_2).read_bytes()
    first, second = resent.whose(first_pdf)

    with open_dashboard(collection.tmp_path, rates, clock, drive) as dashboard:
        sign_in(dashboard)

        assert approve(dashboard, first).status_code == 200

        assert (archive_folder(collection) / NAME).read_bytes() == first_pdf
        assert state_of(collection, second)[0] is EmailState.NEEDS_REVIEW
        assert pending_folder(collection) == [NAME_2]
        assert (pending / NAME_2).read_bytes() == second_pdf


def test_rejecting_the_second_of_two_alike_documents_removes_only_its_copy(
    collection: Collection, rates: FakeExchangeRates, clock: list[datetime]
) -> None:
    resent = attached(collection)
    drive = NumberingDrive()
    hold_in_drive(collection, drive, list(resent.emails))
    pending = archive_folder(collection) / "pending"
    first_pdf = (pending / NAME).read_bytes()
    second, first = resent.whose((pending / NAME_2).read_bytes())

    with open_dashboard(collection.tmp_path, rates, clock, drive) as dashboard:
        sign_in(dashboard)

        response = dashboard.post(action_path(second, "reject"))

        assert response.status_code == 200
        assert response.json()["warnings"] == []
        assert pending_folder(collection) == [NAME]
        assert (pending / NAME).read_bytes() == first_pdf
        assert state_of(collection, first)[0] is EmailState.NEEDS_REVIEW


# Showing a held document filed to Drive


def test_held_document_in_drive_is_shown_from_its_copy_on_this_machine(
    collection: Collection, rates: FakeExchangeRates, clock: list[datetime]
) -> None:
    resent = attached(collection)
    drive = DriveLinkingById()
    hold_in_drive(collection, drive, list(resent.emails))
    pending = archive_folder(collection) / "pending"

    with open_dashboard(collection.tmp_path, rates, clock, drive) as dashboard:
        sign_in(dashboard)

        documents = [document for item in queue(dashboard) for document in item["documents"]]

        # Named as the run named each file, the second with its number, not after the link.
        assert sorted(d["file_name"] for d in documents) == [NAME, NAME_2]
        for document in documents:
            name = document["file_name"]
            assert document["drive_url"] == drive.links[("2026-08/pending", name)]
            # Drive cannot be shown inside the page, so the app serves the copy here.
            digest = document["content_hash"]
            assert document["file_url"] == (
                f"/api/months/2026-08/review/billing-documents/{digest}/{name}"
            )
            # By the document: a name that is neither its copy's nor the run's serves nothing.
            stranger = "2026-08_Figma_190.00-USD_3.pdf"
            assert (
                dashboard.get(
                    f"/api/months/2026-08/review/billing-documents/{digest}/{stranger}"
                ).status_code
                == 404
            )
            response = dashboard.get(document["file_url"])
            assert response.status_code == 200
            assert response.headers["content-type"] == "application/pdf"
            assert response.content == (pending / name).read_bytes()


def test_held_document_in_drive_with_no_copy_here_opens_in_drive(
    collection: Collection, rates: FakeExchangeRates, clock: list[datetime]
) -> None:
    resent = attached(collection)
    drive = DriveLinkingById()
    hold_in_drive(collection, drive, [resent.emails[0]])
    (archive_folder(collection) / "pending" / NAME).unlink()

    with open_dashboard(collection.tmp_path, rates, clock, drive) as dashboard:
        sign_in(dashboard)

        [document] = queue(dashboard)[0]["documents"]

        assert document["file_name"] == NAME
        assert document["file_url"] == drive.links[("2026-08/pending", NAME)]
        assert document["drive_url"] == document["file_url"]
        digest = document["content_hash"]
        assert (
            dashboard.get(
                f"/api/months/2026-08/review/billing-documents/{digest}/{NAME}"
            ).status_code
            == 404
        )


# A ledger written before the saved PDF was recorded


def test_held_document_in_an_older_ledger_is_found_when_one_file_could_be_it(
    collection: Collection, rates: FakeExchangeRates, clock: list[datetime]
) -> None:
    resent = attached(collection)
    drive = NumberingDrive()
    email = resent.emails[0]
    hold_in_drive(collection, drive, [email])
    forget_saved_pdfs(collection)

    with open_dashboard(collection.tmp_path, rates, clock, drive) as dashboard:
        sign_in(dashboard)

        response = approve(dashboard, email)

        assert response.status_code == 200
        assert (archive_folder(collection) / NAME).read_bytes() == resent.marks[0]
        assert pending_folder(collection) == []


def test_held_document_in_an_older_ledger_is_refused_when_several_files_could_be_it(
    collection: Collection, rates: FakeExchangeRates, clock: list[datetime]
) -> None:
    resent = attached(collection)
    drive = NumberingDrive()
    hold_in_drive(collection, drive, list(resent.emails))
    forget_saved_pdfs(collection)
    second, first = resent.whose((archive_folder(collection) / "pending" / NAME_2).read_bytes())

    with open_dashboard(collection.tmp_path, rates, clock, drive) as dashboard:
        sign_in(dashboard)

        response = approve(dashboard, second)

        assert response.status_code == 409
        detail = response.json()["detail"]
        assert f"pending/{NAME}" in detail
        assert f"pending/{NAME_2}" in detail
        assert "Nothing was changed" in detail
        assert state_of(collection, second)[0] is EmailState.NEEDS_REVIEW
        assert state_of(collection, first)[0] is EmailState.NEEDS_REVIEW
        assert pending_folder(collection) == [NAME, NAME_2]
        assert dashboard.get(f"{REVIEW}/history").json() == []


# A document filed on this machine alone is linked to its file


def test_file_changed_since_the_run_saved_it_is_not_filed(
    collection: Collection, rates: FakeExchangeRates, clock: list[datetime]
) -> None:
    resent = attached(collection)
    email = resent.emails[0]
    result = collection.run([email])
    assert len(result.pending) == 1
    held = archive_folder(collection) / "pending" / NAME
    held.write_bytes(b"%PDF-1.7 something else put in its place")

    with open_dashboard(collection.tmp_path, rates, clock) as dashboard:
        sign_in(dashboard)

        response = approve(dashboard, email)

        assert response.status_code == 409
        assert "changed or replaced" in response.json()["detail"]
        assert state_of(collection, email)[0] is EmailState.NEEDS_REVIEW
        assert [p.name for p in archive_folder(collection).iterdir()] == ["pending"]
        assert dashboard.get(f"{REVIEW}/history").json() == []


def test_file_as_the_run_saved_it_is_filed(
    collection: Collection, rates: FakeExchangeRates, clock: list[datetime]
) -> None:
    resent = attached(collection)
    email = resent.emails[0]
    collection.run([email])

    with open_dashboard(collection.tmp_path, rates, clock) as dashboard:
        sign_in(dashboard)

        assert approve(dashboard, email).status_code == 200

        assert (archive_folder(collection) / NAME).read_bytes() == resent.marks[0]


# A decision stands whatever the tidying up meets


def test_approval_is_recorded_when_the_pending_copy_cannot_be_removed_for_any_reason(
    collection: Collection, rates: FakeExchangeRates, clock: list[datetime]
) -> None:
    resent = attached(collection)
    drive = DriveThatCannotBeFound()
    email = resent.emails[0]
    hold_in_drive(collection, drive, [email])

    with open_dashboard(collection.tmp_path, rates, clock, drive) as dashboard:
        sign_in(dashboard)

        response = approve(dashboard, email, total="195.00")

        assert response.status_code == 200
        [warning] = response.json()["warnings"]
        assert "the folder Invoice Collection was not found in Drive" in warning
        assert "The approval stands" in warning
        assert state_of(collection, email) == (EmailState.COLLECTED, None)
        [decision] = dashboard.get(f"{REVIEW}/history").json()
        assert (decision["action"], decision["message_id"]) == ("approved", email.message_id)
        [correction] = corrections(collection)
        assert correction["confirmed"]["total"] == "195.00"


def test_rejection_is_recorded_when_the_pending_copy_cannot_be_removed_for_any_reason(
    collection: Collection, rates: FakeExchangeRates, clock: list[datetime]
) -> None:
    resent = attached(collection)
    drive = DriveThatCannotBeFound()
    email = resent.emails[0]
    hold_in_drive(collection, drive, [email])

    with open_dashboard(collection.tmp_path, rates, clock, drive) as dashboard:
        sign_in(dashboard)

        response = dashboard.post(action_path(email, "reject"))

        assert response.status_code == 200
        [warning] = response.json()["warnings"]
        assert "The rejection stands" in warning
        assert state_of(collection, email)[0] is EmailState.SKIPPED
        [decision] = dashboard.get(f"{REVIEW}/history").json()
        assert (decision["action"], decision["message_id"]) == ("rejected", email.message_id)
