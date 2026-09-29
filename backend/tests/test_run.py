"""Tests at the run seam: a whole collection against in-memory source accounts."""

import csv
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from invoice_collector.archive import LocalArchive
from invoice_collector.domain import Attachment, Email, EmailState, Extraction
from invoice_collector.extractor import FakeExtractor
from invoice_collector.ledger import Ledger
from invoice_collector.mail_source import InMemoryMailSource
from invoice_collector.run import CollectionMonth, RunResult, collect
from invoice_collector.summary import CsvSummary

AUGUST = CollectionMonth(2026, 8)
SLACK_PDF = b"%PDF-1.7 slack invoice"


def slack_email() -> Email:
    return Email(
        source_account="engineering@nyayalabs.example",
        message_id="m-slack-1",
        sender="Slack <feedback@slack.com>",
        subject="Your Slack invoice is available",
        received_at=datetime(2026, 8, 3, 9, 12, tzinfo=UTC),
        attachments=(Attachment("Invoice-SBIE-8841207.pdf", "application/pdf", SLACK_PDF),),
    )


def slack_extraction() -> Extraction:
    return Extraction(
        document_type="invoice",
        vendor="Slack",
        invoice_date=date(2026, 8, 3),
        total=Decimal("652.50"),
        currency="USD",
    )


def test_pdf_attachment_is_collected_into_the_summary(tmp_path: Path) -> None:
    result = collect(
        AUGUST,
        sources=[InMemoryMailSource("engineering@nyayalabs.example", [slack_email()])],
        extractor=FakeExtractor({SLACK_PDF: slack_extraction()}),
        archive=LocalArchive(tmp_path / "archive"),
        ledger=Ledger(tmp_path / "ledger.sqlite"),
        summary_writers=[],
    )

    [row] = result.summary
    assert row.vendor == "Slack"
    assert row.invoice_date == date(2026, 8, 3)
    assert row.total == Decimal("652.50")
    assert row.currency == "USD"
    assert row.source_account == "engineering@nyayalabs.example"
    assert row.file_link == "archive/2026-08/2026-08_Slack_652.50-USD.pdf"


def run_august(tmp_path: Path, emails: list[Email]) -> tuple[RunResult, Ledger]:
    ledger = Ledger(tmp_path / "ledger.sqlite")
    result = collect(
        AUGUST,
        sources=[InMemoryMailSource("engineering@nyayalabs.example", emails)],
        extractor=FakeExtractor({SLACK_PDF: slack_extraction()}),
        archive=LocalArchive(tmp_path / "archive"),
        ledger=ledger,
        summary_writers=[CsvSummary(tmp_path / "summary.csv")],
    )
    return result, ledger


def newsletter() -> Email:
    return Email(
        source_account="engineering@nyayalabs.example",
        message_id="m-news-1",
        sender="Slack <news@slack.com>",
        subject="What is new in Slack",
        received_at=datetime(2026, 8, 10, 8, 0, tzinfo=UTC),
    )


def test_pdf_is_saved_in_the_folder_of_its_collection_month(tmp_path: Path) -> None:
    run_august(tmp_path, [slack_email()])

    saved = tmp_path / "archive" / "2026-08" / "2026-08_Slack_652.50-USD.pdf"
    assert saved.read_bytes() == SLACK_PDF


def test_email_without_a_pdf_is_skipped_with_a_reason(tmp_path: Path) -> None:
    result, ledger = run_august(tmp_path, [newsletter()])

    assert result.summary == []
    [examined] = ledger.examined_emails()
    assert examined.state is EmailState.SKIPPED
    assert examined.reason == "no PDF attachment"


def test_every_email_examined_ends_in_exactly_one_state(tmp_path: Path) -> None:
    _, ledger = run_august(tmp_path, [slack_email(), newsletter()])

    states = {e.message_id: e.state for e in ledger.examined_emails()}
    assert states == {"m-slack-1": EmailState.COLLECTED, "m-news-1": EmailState.SKIPPED}


def test_saved_pdf_traces_back_to_its_source_email(tmp_path: Path) -> None:
    result, ledger = run_august(tmp_path, [slack_email()])

    source = ledger.source_of(result.summary[0].file_link)
    assert source is not None
    assert source.message_id == "m-slack-1"
    assert source.sender == "Slack <feedback@slack.com>"


def test_email_received_outside_the_month_is_not_examined(tmp_path: Path) -> None:
    july = replace(slack_email(), received_at=datetime(2026, 7, 31, 23, 59, tzinfo=UTC))

    result, ledger = run_august(tmp_path, [july])

    assert result.summary == []
    assert ledger.examined_emails() == []


def test_document_that_cannot_be_extracted_fails_without_stopping_the_run(tmp_path: Path) -> None:
    unreadable = replace(
        slack_email(),
        message_id="m-unknown-1",
        attachments=(Attachment("scan.pdf", "application/pdf", b"%PDF unreadable"),),
    )

    result, ledger = run_august(tmp_path, [unreadable, slack_email()])

    assert [row.vendor for row in result.summary] == ["Slack"]
    states = {e.message_id: (e.state, e.reason) for e in ledger.examined_emails()}
    assert states["m-unknown-1"] == (EmailState.FAILED, "no prepared answer for this document")
    assert states["m-slack-1"] == (EmailState.COLLECTED, None)


def test_summary_is_written_as_csv(tmp_path: Path) -> None:
    run_august(tmp_path, [slack_email()])

    with (tmp_path / "summary.csv").open(newline="", encoding="utf-8") as f:
        [row] = list(csv.DictReader(f))
    assert row == {
        "vendor": "Slack",
        "date": "2026-08-03",
        "amount": "652.50",
        "currency": "USD",
        "source_account": "engineering@nyayalabs.example",
        "file_link": "archive/2026-08/2026-08_Slack_652.50-USD.pdf",
    }
