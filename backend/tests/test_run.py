"""Tests at the run seam: a whole collection against in-memory source accounts."""

import csv
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from invoice_collector.archive import LocalArchive
from invoice_collector.domain import (
    Attachment,
    CollectionMonth,
    Email,
    EmailState,
    Extraction,
    InvoiceFormat,
)
from invoice_collector.extractor import FakeExtractor
from invoice_collector.ledger import Ledger
from invoice_collector.mail_source import InMemoryMailSource
from invoice_collector.portal import FakePortalFetcher, LoginGated
from invoice_collector.renderer import FakeRenderer
from invoice_collector.run import Pipeline, RunResult, collect
from invoice_collector.summary import CsvSummary

AUGUST = CollectionMonth(2026, 8)
ENGINEERING = "engineering@nyayalabs.example"

SLACK_PDF = b"%PDF-1.7 slack invoice"
FIGMA_PDF = b"%PDF-1.7 figma invoice"
NOTION_HTML = "<h2>Receipt #2391-7745</h2><p>Total paid €221.40</p>"
NOTION_TEXT = "Receipt from Notion. Total paid EUR 221.40"
NOTION_PDF = FakeRenderer().render_html(NOTION_HTML)
NOTION_TEXT_PDF = FakeRenderer().render_html(f"<pre>{NOTION_TEXT}</pre>")

FIGMA_PORTAL = "https://billing.figma.example/i/in_1PqX7fK2"
LINEAR_PORTAL = "https://linear.example/settings/billing"
BROKEN_PORTAL = "https://gone.example/invoice/1"

SLACK = Extraction("invoice", "Slack", date(2026, 8, 3), Decimal("652.50"), "USD")
FIGMA = Extraction("invoice", "Figma", date(2026, 8, 21), Decimal("190.00"), "USD")
NOTION = Extraction("receipt", "Notion", date(2026, 8, 14), Decimal("221.40"), "EUR")


def pdf(content: bytes, name: str = "invoice.pdf") -> Attachment:
    return Attachment(name, "application/pdf", content)


def slack_email() -> Email:
    return Email(
        source_account=ENGINEERING,
        message_id="m-slack-1",
        sender="Slack <feedback@slack.com>",
        subject="Your Slack invoice is available",
        received_at=datetime(2026, 8, 3, 9, 12, tzinfo=UTC),
        attachments=(pdf(SLACK_PDF),),
    )


def newsletter() -> Email:
    return Email(
        source_account=ENGINEERING,
        message_id="m-news-1",
        sender="Slack <news@slack.com>",
        subject="What is new in Slack",
        received_at=datetime(2026, 8, 10, 8, 0, tzinfo=UTC),
    )


def notion_receipt() -> Email:
    return Email(
        source_account=ENGINEERING,
        message_id="m-notion-1",
        sender="Notion <team@mail.notion.so>",
        subject="Your receipt from Notion #2391-7745",
        received_at=datetime(2026, 8, 14, 17, 40, tzinfo=UTC),
        html_body=NOTION_HTML,
    )


def portal_email(url: str, message_id: str = "m-portal-1") -> Email:
    return Email(
        source_account=ENGINEERING,
        message_id=message_id,
        sender="Figma <billing@figma.com>",
        subject="Your invoice is ready",
        received_at=datetime(2026, 8, 21, 6, 5, tzinfo=UTC),
        html_body=f'<p>Your latest invoice is ready.</p><a href="{url}">View invoice</a>',
    )


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[Ledger]:
    ledger = Ledger(tmp_path / "ledger.sqlite")
    yield ledger
    ledger.close()


Collect = Callable[[list[Email]], RunResult]


def pipeline_reading(answers: dict[bytes, Extraction], tmp_path: Path, ledger: Ledger) -> Pipeline:
    return Pipeline(
        extractor=FakeExtractor.for_documents(answers),
        renderer=FakeRenderer(),
        portal_fetcher=FakePortalFetcher({}),
        archive=LocalArchive(tmp_path / "archive"),
        ledger=ledger,
    )


@pytest.fixture
def collect_august(tmp_path: Path, ledger: Ledger) -> Collect:
    def run(emails: list[Email]) -> RunResult:
        return collect(
            AUGUST,
            sources=[InMemoryMailSource(ENGINEERING, emails)],
            pipeline=Pipeline(
                extractor=FakeExtractor.for_documents(
                    {
                        SLACK_PDF: SLACK,
                        FIGMA_PDF: FIGMA,
                        NOTION_PDF: NOTION,
                        NOTION_TEXT_PDF: NOTION,
                    }
                ),
                renderer=FakeRenderer(),
                portal_fetcher=FakePortalFetcher(
                    {FIGMA_PORTAL: FIGMA_PDF, LINEAR_PORTAL: LoginGated()}
                ),
                archive=LocalArchive(tmp_path / "archive"),
                ledger=ledger,
            ),
            summary_writers=[CsvSummary(tmp_path / "summary.csv")],
        )

    return run


def test_pdf_attachment_is_collected_into_the_summary(collect_august: Collect) -> None:
    result = collect_august([slack_email()])

    [row] = result.summary
    assert row.vendor == "Slack"
    assert row.invoice_date == date(2026, 8, 3)
    assert row.total == Decimal("652.50")
    assert row.currency == "USD"
    assert row.source_account == ENGINEERING
    assert row.file_link == "archive/2026-08/2026-08_Slack_652.50-USD.pdf"


def test_pdf_is_saved_in_the_folder_of_its_collection_month(
    collect_august: Collect, tmp_path: Path
) -> None:
    collect_august([slack_email()])

    saved = tmp_path / "archive" / "2026-08" / "2026-08_Slack_652.50-USD.pdf"
    assert saved.read_bytes() == SLACK_PDF


def test_pdf_with_a_generic_content_type_is_still_collected(collect_august: Collect) -> None:
    generic = replace(
        slack_email(),
        attachments=(Attachment("Invoice.PDF", "application/octet-stream", SLACK_PDF),),
    )

    result = collect_august([generic])

    assert [row.vendor for row in result.summary] == ["Slack"]


def test_every_pdf_in_an_email_is_collected(collect_august: Collect) -> None:
    both = replace(slack_email(), attachments=(pdf(SLACK_PDF), pdf(FIGMA_PDF)))

    result = collect_august([both])

    assert [row.vendor for row in result.summary] == ["Slack", "Figma"]


def test_invoice_in_an_email_body_is_saved_as_a_pdf(
    collect_august: Collect, ledger: Ledger, tmp_path: Path
) -> None:
    result = collect_august([notion_receipt()])

    [row] = result.summary
    assert (row.vendor, row.total, row.currency) == ("Notion", Decimal("221.40"), "EUR")
    assert (tmp_path / row.file_link).read_bytes() == NOTION_PDF
    [examined] = ledger.examined_emails(AUGUST)
    assert examined.invoice_format is InvoiceFormat.BODY


def test_plain_text_invoice_in_an_email_body_is_collected(collect_august: Collect) -> None:
    plain = replace(notion_receipt(), html_body=None, text_body=NOTION_TEXT)

    result = collect_august([plain])

    assert [row.vendor for row in result.summary] == ["Notion"]


def test_invoice_behind_a_tokenised_portal_link_is_collected(
    collect_august: Collect, ledger: Ledger, tmp_path: Path
) -> None:
    result = collect_august([portal_email(FIGMA_PORTAL)])

    [row] = result.summary
    assert row.vendor == "Figma"
    assert (tmp_path / row.file_link).read_bytes() == FIGMA_PDF
    [examined] = ledger.examined_emails(AUGUST)
    assert examined.invoice_format is InvoiceFormat.PORTAL_LINK
    assert examined.portal_link == FIGMA_PORTAL


def test_login_gated_portal_link_is_flagged_for_manual_download(
    collect_august: Collect, ledger: Ledger
) -> None:
    result = collect_august([portal_email(LINEAR_PORTAL)])

    assert result.summary == []
    [examined] = ledger.examined_emails(AUGUST)
    assert examined.state is EmailState.NEEDS_REVIEW
    assert examined.reason == "manual download needed"
    assert examined.portal_link == LINEAR_PORTAL


def test_portal_link_that_cannot_be_opened_fails_without_stopping_the_run(
    collect_august: Collect, ledger: Ledger
) -> None:
    result = collect_august([portal_email(BROKEN_PORTAL, "m-broken-1"), slack_email()])

    assert [row.vendor for row in result.summary] == ["Slack"]
    states = {e.message_id: (e.state, e.reason) for e in ledger.examined_emails(AUGUST)}
    assert states["m-broken-1"] == (EmailState.FAILED, f"could not open {BROKEN_PORTAL}")


def test_attached_pdf_is_preferred_over_the_email_body(
    collect_august: Collect, ledger: Ledger
) -> None:
    both = replace(slack_email(), html_body="<p>Invoice total $652.50</p>")

    collect_august([both])

    [examined] = ledger.examined_emails(AUGUST)
    assert examined.invoice_format is InvoiceFormat.ATTACHMENT


def test_promotion_that_mentions_a_price_is_skipped(
    collect_august: Collect, ledger: Ledger
) -> None:
    promotion = replace(newsletter(), html_body="<p>Upgrade today for only $8.00 a month</p>")

    collect_august([promotion])

    [examined] = ledger.examined_emails(AUGUST)
    assert examined.state is EmailState.SKIPPED


def test_email_with_no_billing_document_is_skipped_with_a_reason(
    collect_august: Collect, ledger: Ledger
) -> None:
    result = collect_august([newsletter()])

    assert result.summary == []
    [examined] = ledger.examined_emails(AUGUST)
    assert examined.state is EmailState.SKIPPED
    assert examined.reason == "no billing document found"


def test_every_email_examined_ends_in_exactly_one_state(
    collect_august: Collect, ledger: Ledger
) -> None:
    collect_august([slack_email(), newsletter(), portal_email(LINEAR_PORTAL)])

    states = {e.message_id: e.state for e in ledger.examined_emails(AUGUST)}
    assert states == {
        "m-slack-1": EmailState.COLLECTED,
        "m-news-1": EmailState.SKIPPED,
        "m-portal-1": EmailState.NEEDS_REVIEW,
    }


def test_saved_pdf_traces_back_to_its_source_email(collect_august: Collect, ledger: Ledger) -> None:
    result = collect_august([slack_email()])

    source = ledger.source_of(result.summary[0].file_link)
    assert source is not None
    assert source.message_id == "m-slack-1"
    assert source.sender == "Slack <feedback@slack.com>"


def test_email_received_outside_the_month_is_not_examined(
    collect_august: Collect, ledger: Ledger
) -> None:
    july = replace(slack_email(), received_at=datetime(2026, 7, 31, 23, 59, tzinfo=UTC))

    result = collect_august([july])

    assert result.summary == []
    assert ledger.examined_emails(AUGUST) == []


def test_document_that_cannot_be_extracted_fails_without_stopping_the_run(
    collect_august: Collect, ledger: Ledger
) -> None:
    unreadable = replace(
        slack_email(), message_id="m-unknown-1", attachments=(pdf(b"%PDF-1.7 unreadable"),)
    )

    result = collect_august([unreadable, slack_email()])

    assert [row.vendor for row in result.summary] == ["Slack"]
    states = {e.message_id: (e.state, e.reason) for e in ledger.examined_emails(AUGUST)}
    assert states["m-unknown-1"] == (EmailState.FAILED, "no prepared answer for this document")
    assert states["m-slack-1"] == (EmailState.COLLECTED, None)


def test_running_a_month_twice_adds_no_rows(collect_august: Collect) -> None:
    collect_august([slack_email()])

    result = collect_august([slack_email()])

    assert [row.vendor for row in result.summary] == ["Slack"]


def test_two_documents_that_share_a_name_are_both_kept(tmp_path: Path, ledger: Ledger) -> None:
    first, second = b"%PDF-1.7 slack invoice A", b"%PDF-1.7 slack invoice B"
    emails = [
        replace(slack_email(), message_id="m-a", attachments=(pdf(first),)),
        replace(slack_email(), message_id="m-b", attachments=(pdf(second),)),
    ]

    result = collect(
        AUGUST,
        sources=[InMemoryMailSource(ENGINEERING, emails)],
        pipeline=pipeline_reading({first: SLACK, second: SLACK}, tmp_path, ledger),
        summary_writers=[],
    )

    links = sorted(row.file_link for row in result.summary)
    assert links == [
        "archive/2026-08/2026-08_Slack_652.50-USD.pdf",
        "archive/2026-08/2026-08_Slack_652.50-USD_2.pdf",
    ]
    assert {(tmp_path / link).read_bytes() for link in links} == {first, second}


def test_running_a_month_twice_saves_each_document_once(
    collect_august: Collect, tmp_path: Path
) -> None:
    collect_august([slack_email()])
    collect_august([slack_email()])

    saved = [p.name for p in (tmp_path / "archive" / "2026-08").iterdir()]
    assert saved == ["2026-08_Slack_652.50-USD.pdf"]


def test_same_pdf_attached_twice_to_an_email_is_collected_once(
    collect_august: Collect, ledger: Ledger
) -> None:
    twice = replace(
        slack_email(), attachments=(pdf(SLACK_PDF, "invoice.pdf"), pdf(SLACK_PDF, "copy.pdf"))
    )

    result = collect_august([twice])

    assert [row.vendor for row in result.summary] == ["Slack"]
    [examined] = ledger.examined_emails(AUGUST)
    assert examined.state is EmailState.COLLECTED


def test_text_that_a_spreadsheet_would_run_as_a_formula_is_neutralised(
    tmp_path: Path, ledger: Ledger
) -> None:
    hostile = replace(SLACK, vendor='=HYPERLINK("https://evil.example","Slack")')

    collect(
        AUGUST,
        sources=[InMemoryMailSource(ENGINEERING, [slack_email()])],
        pipeline=pipeline_reading({SLACK_PDF: hostile}, tmp_path, ledger),
        summary_writers=[CsvSummary(tmp_path / "summary.csv")],
    )

    with (tmp_path / "summary.csv").open(newline="", encoding="utf-8") as f:
        [row] = list(csv.DictReader(f))
    assert row["vendor"] == '\'=HYPERLINK("https://evil.example","Slack")'
    assert row["amount"] == "652.50"


def test_summary_is_written_as_csv(collect_august: Collect, tmp_path: Path) -> None:
    collect_august([slack_email()])

    with (tmp_path / "summary.csv").open(newline="", encoding="utf-8") as f:
        [row] = list(csv.DictReader(f))
    assert row == {
        "vendor": "Slack",
        "date": "2026-08-03",
        "amount": "652.50",
        "currency": "USD",
        "source_account": ENGINEERING,
        "file_link": "archive/2026-08/2026-08_Slack_652.50-USD.pdf",
    }
