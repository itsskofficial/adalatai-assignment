"""The command line runs a collection against a folder of sample emails."""

import csv
import hashlib
import json
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

import pytest
from fake_google import FakeDrive, FakeSheets

from invoice_collector.cli import main

PDF = b"%PDF-1.7 figma invoice"


def write_samples(root: Path) -> None:
    message = EmailMessage()
    message["From"] = "Figma <billing@figma.com>"
    message["To"] = "ops@nyayalabs.example"
    message["Subject"] = "Your Figma invoice"
    message["Date"] = format_datetime(datetime(2026, 8, 21, 6, 5, tzinfo=UTC))
    message["Message-ID"] = "<figma-1@figma.com>"
    message.set_content("Your invoice is attached.")
    message.add_attachment(PDF, maintype="application", subtype="pdf", filename="invoice.pdf")

    account = root / "ops@nyayalabs.example"
    account.mkdir(parents=True)
    (account / "figma.eml").write_bytes(bytes(message))
    answers = {
        hashlib.sha256(PDF).hexdigest(): {
            "document_type": "invoice",
            "vendor": "Figma",
            "invoice_date": "2026-08-21",
            "total": "190.00",
            "currency": "USD",
        }
    }
    (root / "answers.json").write_text(json.dumps(answers), encoding="utf-8")


def test_collect_command_produces_an_archive_and_a_summary(tmp_path: Path) -> None:
    samples, out = tmp_path / "samples", tmp_path / "out"
    write_samples(samples)

    exit_code = main(
        ["collect", "2026-08", "--samples", str(samples), "--out", str(out)]
        + ["--extractor", "prepared", "--classifier", "rules", "--no-exchange-rates"]
    )

    assert exit_code == 0
    assert (out / "archive/2026-08/2026-08_Figma_190.00-USD.pdf").read_bytes() == PDF
    with (out / "2026-08_summary.csv").open(newline="", encoding="utf-8") as f:
        [row] = list(csv.DictReader(f))
    assert row["vendor"] == "Figma"
    assert row["source_account"] == "ops@nyayalabs.example"


OWNER = "ops@nyayalabs.example"
OFFLINE = ["--extractor", "prepared", "--classifier", "rules", "--no-exchange-rates"]


def store_owner_sign_in(token_dir: Path) -> None:
    token_dir.mkdir(parents=True)
    (token_dir / f"{OWNER}.json").write_text(
        json.dumps(
            {
                "token": "stored-access-value",
                "refresh_token": "stored-refresh-value",
                "token_uri": "http://127.0.0.1:9/never-called",
                "client_id": "made-up-client.apps.googleusercontent.com",
                "client_secret": "made-up",
                "scopes": ["https://www.googleapis.com/auth/drive.file"],
                "expiry": "2999-01-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )


def test_collect_command_refuses_to_run_when_the_owner_account_is_not_signed_in(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    samples, out = tmp_path / "samples", tmp_path / "out"
    write_samples(samples)

    exit_code = main(
        ["collect", "2026-08", "--samples", str(samples), "--out", str(out)]
        + ["--google-owner", OWNER, "--token-dir", str(tmp_path / "tokens")]
        + OFFLINE
    )

    assert exit_code == 1
    assert f"invoice-collector-setup --owner {OWNER}" in capsys.readouterr().err
    assert not out.exists()


def test_collect_command_with_an_owner_archives_to_drive_and_writes_the_sheet(
    tmp_path: Path,
) -> None:
    samples, out, tokens = tmp_path / "samples", tmp_path / "out", tmp_path / "tokens"
    write_samples(samples)
    store_owner_sign_in(tokens)
    drive = FakeDrive()
    sheets = FakeSheets(drive)

    exit_code = main(
        ["collect", "2026-08", "--samples", str(samples), "--out", str(out)]
        + ["--google-owner", OWNER, "--token-dir", str(tokens)]
        + OFFLINE,
        google_services=lambda credentials: (drive, sheets),
    )

    assert exit_code == 0
    [uploaded] = drive.named("2026-08_Figma_190.00-USD.pdf")
    assert uploaded.content == PDF
    assert (out / "archive/2026-08/2026-08_Figma_190.00-USD.pdf").read_bytes() == PDF
    with (out / "2026-08_summary.csv").open(newline="", encoding="utf-8") as f:
        [row] = list(csv.DictReader(f))
    assert row["file_link"] == uploaded.link
    [sheet] = drive.named("Invoice summary 2026-08")
    assert sheets.tab(sheet.id, "Summary").rows[1][0] == "Figma"
