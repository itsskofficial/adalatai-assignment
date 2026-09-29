"""Saving one PDF to two archives at once."""

from pathlib import Path

from fake_google import FakeDrive

from invoice_collector.archive import Archive, BothArchives, LocalArchive
from invoice_collector.drive_archive import DriveArchive

PDF = b"%PDF-1.7 figma invoice"
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
