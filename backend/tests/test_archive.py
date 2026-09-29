"""Saving one PDF to two archives at once."""

from pathlib import Path

import pytest
from fake_google import FakeDrive

from invoice_collector.archive import Archive, BothArchives, LocalArchive
from invoice_collector.drive_archive import DriveArchive

PDF = b"%PDF-1.7 figma invoice"
OTHER_PDF = b"%PDF-1.7 another figma invoice"
NAME = "2026-08_Figma_190.00-USD.pdf"


def test_both_archives_keep_the_pdf_and_the_link_is_that_of_the_first(tmp_path: Path) -> None:
    drive = FakeDrive()
    archive: Archive = BothArchives(DriveArchive(drive), LocalArchive(tmp_path / "archive"))

    link = archive.save("2026-08", NAME, PDF)

    [uploaded] = drive.named(NAME)
    assert link == uploaded.link
    assert (tmp_path / "archive" / "2026-08" / NAME).read_bytes() == PDF


def test_saving_again_to_both_archives_adds_nothing_to_either(tmp_path: Path) -> None:
    drive = FakeDrive()
    archive = BothArchives(DriveArchive(drive), LocalArchive(tmp_path / "archive"))
    first = archive.save("2026-08", NAME, PDF)

    again = archive.save("2026-08", NAME, PDF)

    assert again == first
    assert len(drive.named(NAME)) == 1
    assert [p.name for p in (tmp_path / "archive" / "2026-08").iterdir()] == [NAME]


def test_pdf_is_kept_locally_when_the_first_archive_cannot_be_reached(tmp_path: Path) -> None:
    archive = BothArchives(Unreachable(), LocalArchive(tmp_path / "archive"))

    with pytest.raises(ConnectionError, match="Drive could not be reached"):
        archive.save("2026-08", "2026-08_Slack_652.50-USD.pdf", b"%PDF-1.7 slack")

    kept = tmp_path / "archive" / "2026-08" / "2026-08_Slack_652.50-USD.pdf"
    assert kept.read_bytes() == b"%PDF-1.7 slack"


# Removing a copy, as the Review screen does with a held document's pending copy


class Unreachable:
    """An archive that cannot be reached."""

    def save(self, folder: str, filename: str, pdf: bytes) -> str:
        raise ConnectionError("Drive could not be reached")

    def remove(self, folder: str, filename: str, pdf: bytes) -> None:
        raise ConnectionError("Drive could not be reached")


def test_removing_from_both_archives_takes_the_copy_from_each(tmp_path: Path) -> None:
    drive = FakeDrive()
    archive = BothArchives(DriveArchive(drive), LocalArchive(tmp_path / "archive"))
    archive.save("2026-08/pending", NAME, PDF)

    archive.remove("2026-08/pending", NAME, PDF)

    assert [f.name for f in drive.trashed] == [NAME]
    assert not (tmp_path / "archive" / "2026-08" / "pending").exists()


def test_copy_is_removed_from_the_second_archive_when_the_first_cannot_be_reached(
    tmp_path: Path,
) -> None:
    local = LocalArchive(tmp_path / "archive")
    local.save("2026-08/pending", NAME, PDF)

    with pytest.raises(ConnectionError):
        BothArchives(Unreachable(), local).remove("2026-08/pending", NAME, PDF)

    assert not (tmp_path / "archive" / "2026-08" / "pending").exists()


def test_local_archive_removes_only_the_copy_of_that_pdf(tmp_path: Path) -> None:
    archive = LocalArchive(tmp_path / "archive")
    archive.save("2026-08/pending", NAME, PDF)
    archive.save("2026-08/pending", NAME, OTHER_PDF)

    archive.remove("2026-08/pending", NAME, OTHER_PDF)

    folder = tmp_path / "archive" / "2026-08" / "pending"
    assert [p.name for p in folder.iterdir()] == [NAME]
    assert (folder / NAME).read_bytes() == PDF


def test_local_archive_finds_a_numbered_copy_after_the_first_was_removed(
    tmp_path: Path,
) -> None:
    archive = LocalArchive(tmp_path / "archive")
    archive.save("2026-08/pending", NAME, PDF)
    archive.save("2026-08/pending", NAME, OTHER_PDF)
    archive.remove("2026-08/pending", NAME, PDF)

    archive.remove("2026-08/pending", NAME, OTHER_PDF)

    assert not (tmp_path / "archive" / "2026-08" / "pending").exists()


def test_removing_a_copy_that_is_not_there_changes_nothing(tmp_path: Path) -> None:
    archive = LocalArchive(tmp_path / "archive")
    archive.save("2026-08/pending", NAME, PDF)

    archive.remove("2026-08/pending", NAME, OTHER_PDF)
    archive.remove("2026-09/pending", NAME, PDF)

    assert (tmp_path / "archive" / "2026-08" / "pending" / NAME).read_bytes() == PDF
