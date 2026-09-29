"""Tests at the run seam: a whole collection against in-memory source accounts."""

import csv
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from invoice_collector.archive import LocalArchive
from invoice_collector.classifier import FakeClassifier
from invoice_collector.domain import (
    Attachment,
    Classification,
    CollectionMonth,
    Email,
    EmailState,
    Extraction,
    InvoiceFormat,
)
from invoice_collector.exchange_rates import FakeExchangeRates
from invoice_collector.extractor import FakeExtractor, FallbackExtractor
from invoice_collector.ledger import Ledger
from invoice_collector.mail_source import InMemoryMailSource
from invoice_collector.portal import FakePortalFetcher, LoginGated
from invoice_collector.renderer import FakeRenderer
from invoice_collector.run import Pipeline, RunResult, collect
from invoice_collector.summary import CsvSummary

AUGUST = CollectionMonth(2026, 8)
ENGINEERING = "engineering@nyayalabs.example"
RATES = {"USD": Decimal("95.34"), "EUR": Decimal("110.3818")}

SLACK_PDF = b"%PDF-1.7 slack invoice"
FIGMA_PDF = b"%PDF-1.7 figma invoice"
TERMS_PDF = b"%PDF-1.7 updated terms of service"
ZOOM_PDF = b"%PDF-1.7 zoom invoice"
CREDIT_PDF = b"%PDF-1.7 slack credit note"
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
CREDIT = Extraction("credit_note", "Slack", date(2026, 8, 18), Decimal("45.00"), "USD")
ZOOM = Extraction("invoice", "Zoom", date(2026, 8, 9), Decimal("149.90"), "USD", "low")


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
        classifier=FakeClassifier(),
        extractor=FakeExtractor.for_documents(answers),
        renderer=FakeRenderer(),
        portal_fetcher=FakePortalFetcher({}),
        exchange_rates=FakeExchangeRates(RATES),
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
                classifier=FakeClassifier(
                    {"m-judged-1": Classification("not_billing", None, "high")},
                    failing=frozenset({"m-unclassified-1"}),
                ),
                extractor=FallbackExtractor(
                    FakeExtractor.for_documents(
                        {
                            SLACK_PDF: SLACK,
                            FIGMA_PDF: FIGMA,
                            NOTION_PDF: NOTION,
                            NOTION_TEXT_PDF: NOTION,
                            CREDIT_PDF: CREDIT,
                        },
                        not_billing=(TERMS_PDF,),
                    ),
                    FakeExtractor.for_documents({ZOOM_PDF: ZOOM}),
                ),
                renderer=FakeRenderer(),
                portal_fetcher=FakePortalFetcher(
                    {FIGMA_PORTAL: FIGMA_PDF, LINEAR_PORTAL: LoginGated()}
                ),
                exchange_rates=FakeExchangeRates(RATES),
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


def test_email_that_is_not_about_billing_is_skipped_with_a_reason(
    collect_august: Collect, ledger: Ledger
) -> None:
    result = collect_august([newsletter()])

    assert result.summary == []
    [examined] = ledger.examined_emails(AUGUST)
    assert examined.state is EmailState.SKIPPED
    assert examined.reason == "not a billing email"


def test_billing_email_with_nothing_to_collect_is_skipped(
    collect_august: Collect, ledger: Ledger
) -> None:
    empty = replace(slack_email(), attachments=())

    collect_august([empty])

    [examined] = ledger.examined_emails(AUGUST)
    assert (examined.state, examined.reason) == (
        EmailState.SKIPPED,
        "no billing document found",
    )


def test_payment_failed_notice_is_recorded_as_a_billing_signal(
    collect_august: Collect, ledger: Ledger, tmp_path: Path
) -> None:
    notice = Email(
        source_account=ENGINEERING,
        message_id="m-failed-1",
        sender="Notion <team@mail.notion.so>",
        subject="Your payment failed",
        received_at=datetime(2026, 8, 12, 4, 0, tzinfo=UTC),
        text_body="We could not process your payment of $190.00 for Notion Plus.",
    )

    result = collect_august([notice])

    assert result.summary == []
    assert not (tmp_path / "archive").exists()
    [signal] = ledger.billing_signals(AUGUST)
    assert (signal.kind, signal.vendor) == ("payment_failed", "Notion")
    [examined] = ledger.examined_emails(AUGUST)
    assert (examined.state, examined.reason) == (
        EmailState.SKIPPED,
        "billing signal: payment failed",
    )


def test_renewal_reminder_is_recorded_as_a_billing_signal(
    collect_august: Collect, ledger: Ledger
) -> None:
    reminder = Email(
        source_account=ENGINEERING,
        message_id="m-renewal-1",
        sender="GitHub <billing@github.com>",
        subject="Your GitHub plan renews on 1 September",
        received_at=datetime(2026, 8, 25, 4, 0, tzinfo=UTC),
        text_body="Your annual plan will renew for $2,520.00. No action is needed.",
    )

    result = collect_august([reminder])

    assert result.summary == []
    [signal] = ledger.billing_signals(AUGUST)
    assert (signal.kind, signal.vendor) == ("renewal_reminder", "GitHub")


def test_credit_note_is_recorded_with_a_negative_amount(collect_august: Collect) -> None:
    credit = replace(
        slack_email(),
        message_id="m-credit-1",
        subject="Your Slack credit note",
        attachments=(pdf(CREDIT_PDF),),
    )

    result = collect_august([credit])

    [row] = result.summary
    assert (row.document_type, row.total) == ("credit_note", Decimal("-45.00"))
    assert row.file_link == "archive/2026-08/2026-08_Slack_-45.00-USD.pdf"


def test_summary_shows_the_document_type(collect_august: Collect) -> None:
    result = collect_august([slack_email(), notion_receipt()])

    types = {row.vendor: row.document_type for row in result.summary}
    assert types == {"Slack": "invoice", "Notion": "receipt"}


def test_email_the_classifier_judges_not_to_be_billing_is_skipped(
    collect_august: Collect, ledger: Ledger
) -> None:
    judged = replace(slack_email(), message_id="m-judged-1")

    result = collect_august([judged])

    assert result.summary == []
    [examined] = ledger.examined_emails(AUGUST)
    assert examined.state is EmailState.SKIPPED


def test_email_that_cannot_be_classified_fails_without_stopping_the_run(
    collect_august: Collect, ledger: Ledger
) -> None:
    unclassified = replace(slack_email(), message_id="m-unclassified-1")

    result = collect_august([unclassified, slack_email()])

    assert [row.vendor for row in result.summary] == ["Slack"]
    states = {e.message_id: (e.state, e.reason) for e in ledger.examined_emails(AUGUST)}
    assert states["m-unclassified-1"] == (
        EmailState.FAILED,
        "the classifier is unavailable",
    )


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


def test_email_received_well_outside_the_month_is_not_examined(
    collect_august: Collect, ledger: Ledger
) -> None:
    july = replace(slack_email(), received_at=datetime(2026, 7, 20, 23, 59, tzinfo=UTC))

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
    assert states["m-unknown-1"] == (
        EmailState.FAILED,
        "no prepared answer for this document; then no prepared answer for this document",
    )
    assert states["m-slack-1"] == (EmailState.COLLECTED, None)


def test_document_the_first_extractor_cannot_read_falls_back_to_the_next(
    collect_august: Collect,
) -> None:
    zoom = replace(slack_email(), message_id="m-zoom-1", attachments=(pdf(ZOOM_PDF),))

    result = collect_august([zoom])

    assert [row.vendor for row in result.summary] == ["Zoom"]


def test_attachment_that_is_not_a_billing_document_is_skipped(
    collect_august: Collect, ledger: Ledger
) -> None:
    terms = replace(slack_email(), message_id="m-terms-1", attachments=(pdf(TERMS_PDF),))

    result = collect_august([terms])

    assert result.summary == []
    [examined] = ledger.examined_emails(AUGUST)
    assert examined.state is EmailState.SKIPPED
    assert examined.reason == "not a billing document"


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
        "document_type": "invoice",
        "amount_inr": "62209.35",
        "inr_rate": "95.34",
        "notes": "",
    }


def test_link_that_does_not_mention_billing_is_not_followed(
    collect_august: Collect, ledger: Ledger
) -> None:
    only_unsubscribe = replace(
        portal_email("https://mail.figma.example/unsubscribe?u=81c"),
        html_body="<p>Your invoice is ready in your account.</p>"
        '<a href="https://mail.figma.example/unsubscribe?u=81c">Unsubscribe</a>',
    )

    result = collect_august([only_unsubscribe])

    assert result.summary == []
    [examined] = ledger.examined_emails(AUGUST)
    assert (examined.state, examined.reason) == (EmailState.SKIPPED, "no billing document found")
    assert examined.portal_link is None
