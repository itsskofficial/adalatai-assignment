"""The archive in Google Drive, checked against a fake Drive and recorded responses."""

import json
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

from fake_google import FOLDER, FakeDrive
from googleapiclient.discovery import build  # pyright: ignore[reportUnknownVariableType]
from googleapiclient.http import HttpMockSequence

from invoice_collector.archive import Archive
from invoice_collector.drive_archive import SCOPES, DriveArchive

RECORDED = Path(__file__).parent / "recorded" / "drive"
PDF = b"%PDF-1.7 figma invoice"
OTHER_PDF = b"%PDF-1.7 another figma invoice"
NAME = "2026-08_Figma_190.00-USD.pdf"


def folder(drive: FakeDrive, name: str, parent: str = "root") -> str:
    [found] = [f for f in drive.named(name) if f.parent == parent and f.mime_type == FOLDER]
    return found.id


def test_drive_archive_asks_only_for_the_files_the_tool_creates() -> None:
    assert SCOPES == ("https://www.googleapis.com/auth/drive.file",)


def test_pdf_is_uploaded_into_the_collection_month_folder_under_the_root_folder() -> None:
    drive = FakeDrive()
    archive: Archive = DriveArchive(drive)

    link = archive.save("2026-08", NAME, PDF)

    month = folder(drive, "2026-08", folder(drive, "Invoice Collection"))
    [pdf] = drive.in_folder(month)
    assert (pdf.name, pdf.content, pdf.mime_type) == (NAME, PDF, "application/pdf")
    assert link == pdf.link


def test_root_folder_can_be_named() -> None:
    drive = FakeDrive()

    DriveArchive(drive, root_folder="Acme invoices").save("2026-08", NAME, PDF)

    assert folder(drive, "Acme invoices")


def test_folders_are_created_once_and_reused() -> None:
    drive = FakeDrive()
    archive = DriveArchive(drive)

    archive.save("2026-08", NAME, PDF)
    queries = len(drive.queries)
    archive.save("2026-08", "2026-08_Slack_8.75-USD.pdf", b"%PDF-1.7 slack")

    assert [f.name for f in drive.created if f.mime_type == FOLDER] == [
        "Invoice Collection",
        "2026-08",
    ]
    # The second save only looks for the file: the folder ids are remembered.
    assert len(drive.queries) == queries + 1


def test_folders_made_by_an_earlier_run_are_found_and_reused() -> None:
    drive = FakeDrive()
    DriveArchive(drive).save("2026-08", NAME, PDF)

    DriveArchive(drive).save("2026-08", "2026-08_Slack_8.75-USD.pdf", b"%PDF-1.7 slack")

    assert len([f for f in drive.files_by_id.values() if f.mime_type == FOLDER]) == 2


def test_each_level_of_a_nested_folder_is_created() -> None:
    drive = FakeDrive()

    DriveArchive(drive).save("2026-08/pending", NAME, PDF)

    month = folder(drive, "2026-08", folder(drive, "Invoice Collection"))
    [pdf] = drive.in_folder(folder(drive, "pending", month))
    assert pdf.name == NAME


def test_identical_document_already_in_the_folder_is_reused_without_uploading() -> None:
    drive = FakeDrive()
    first = DriveArchive(drive).save("2026-08", NAME, PDF)
    uploads = len(drive.created)

    again = DriveArchive(drive).save("2026-08", NAME, PDF)

    assert again == first
    assert len(drive.created) == uploads


def test_different_document_under_the_same_name_gets_a_numbered_suffix() -> None:
    drive = FakeDrive()
    archive = DriveArchive(drive)
    first = archive.save("2026-08", NAME, PDF)

    second = archive.save("2026-08", NAME, OTHER_PDF)
    third = archive.save("2026-08", NAME, b"%PDF-1.7 a third figma invoice")

    assert len({first, second, third}) == 3
    names = sorted(f.name for f in drive.created if f.mime_type == "application/pdf")
    assert names == [NAME, NAME.replace(".pdf", "_2.pdf"), NAME.replace(".pdf", "_3.pdf")]


def test_document_under_a_suffixed_name_is_reused_when_saved_again() -> None:
    drive = FakeDrive()
    archive = DriveArchive(drive)
    archive.save("2026-08", NAME, PDF)
    second = archive.save("2026-08", NAME, OTHER_PDF)
    uploads = len(drive.created)

    assert archive.save("2026-08", NAME, OTHER_PDF) == second
    assert len(drive.created) == uploads


def test_quotes_and_backslashes_in_a_name_are_escaped_in_the_drive_query() -> None:
    drive = FakeDrive()
    name = "2026-08_O'Reilly\\Media_10.00-USD.pdf"

    DriveArchive(drive, root_folder="Nyaya's invoices").save("2026-08", name, PDF)

    assert "name = 'Nyaya\\'s invoices'" in drive.queries[0]
    assert "name = '2026-08_O\\'Reilly\\\\Media_10.00-USD.pdf'" in drive.queries[-1]
    assert [f.name for f in drive.created][-1] == name


def recorded(name: str) -> tuple[dict[str, str], str]:
    return {"status": "200"}, (RECORDED / f"{name}.json").read_text("utf-8")


def test_pdf_is_uploaded_through_the_drive_client_as_recorded() -> None:
    http = HttpMockSequence(
        [
            recorded("root_folder_found"),
            recorded("month_folder_found"),
            recorded("no_files"),
            recorded("pdf_uploaded"),
        ]
    )
    service = cast(Any, build("drive", "v3", http=http))

    link = DriveArchive(service).save("2026-08", NAME, PDF)

    assert link == "https://drive.google.com/file/d/1pdfFigmaAug/view?usp=drivesdk"
    requests = cast(
        "list[tuple[str, str, bytes, dict[str, str]]]",
        http.request_sequence,  # pyright: ignore[reportUnknownMemberType]
    )
    queries = [parse_qs(urlparse(r[0]).query).get("q", [""])[0] for r in requests]
    assert "'1rootFolder' in parents" in queries[1]
    upload_url, method, body = requests[3][0], requests[3][1], requests[3][2]
    assert (urlparse(upload_url).path, method) == ("/upload/drive/v3/files", "POST")
    assert PDF in body
    assert b'"parents": ["1monthFolder"]' in body


# Removing a copy


def test_copy_of_the_pdf_is_moved_to_the_bin() -> None:
    drive = FakeDrive()
    archive = DriveArchive(drive)
    archive.save("2026-08/pending", NAME, PDF)

    archive.remove("2026-08/pending", NAME, PDF)

    [trashed] = drive.trashed
    assert (trashed.name, trashed.content) == (NAME, PDF)
    month = folder(drive, "2026-08", folder(drive, "Invoice Collection"))
    assert drive.in_folder(folder(drive, "pending", month)) == []


def test_different_document_under_the_name_is_left_where_it_is() -> None:
    drive = FakeDrive()
    archive = DriveArchive(drive)
    archive.save("2026-08/pending", NAME, PDF)
    archive.save("2026-08/pending", NAME, OTHER_PDF)

    archive.remove("2026-08/pending", NAME, OTHER_PDF)

    assert [(f.name, f.content) for f in drive.trashed] == [
        (NAME.replace(".pdf", "_2.pdf"), OTHER_PDF)
    ]
    assert [f.content for f in drive.named(NAME) if not f.trashed] == [PDF]


def test_removing_a_copy_that_is_not_in_drive_changes_nothing() -> None:
    drive = FakeDrive()
    archive = DriveArchive(drive)
    archive.save("2026-08/pending", NAME, PDF)

    archive.remove("2026-08/pending", NAME, OTHER_PDF)

    assert drive.trashed == []


def test_copy_is_moved_to_the_bin_through_the_drive_client_as_recorded() -> None:
    http = HttpMockSequence(
        [
            recorded("root_folder_found"),
            recorded("month_folder_found"),
            recorded("pdf_found"),
            recorded("pdf_trashed"),
        ]
    )
    service = cast(Any, build("drive", "v3", http=http))

    DriveArchive(service).remove("2026-08", NAME, PDF)

    requests = cast(
        "list[tuple[str, str, bytes, dict[str, str]]]",
        http.request_sequence,  # pyright: ignore[reportUnknownMemberType]
    )
    listing = parse_qs(urlparse(requests[2][0]).query)["q"][0]
    assert "'1monthFolder' in parents" in listing
    assert "trashed = false" in listing
    url, method, body = requests[3][0], requests[3][1], requests[3][2]
    assert (urlparse(url).path, method) == ("/drive/v3/files/1pdfFigmaAug", "PATCH")
    assert json.loads(body) == {"trashed": True}
