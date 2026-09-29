"""The summary in a Google Sheet, checked against fake Drive and Sheets services."""

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from fake_google import FOLDER, SPREADSHEET, FakeDrive, FakeSheets

from invoice_collector.domain import (
    BillingSignal,
    CollectionMonth,
    Email,
    EmailState,
    InvoiceFormat,
    SummaryRow,
    Sync,
)
from invoice_collector.ledger import ExaminedEmail, Ledger
from invoice_collector.sheet_summary import MonthReport, SheetSummary, month_report
from invoice_collector.summary import COLUMNS, SummaryWriter

AUGUST = CollectionMonth(2026, 8)
TABS = ["Summary", "Pending review", "Skipped and failed", "Billing signals"]

FIGMA = SummaryRow(
    vendor="Figma",
    document_type="invoice",
    invoice_date=date(2026, 8, 21),
    total=Decimal("190.00"),
    currency="USD",
    source_accounts=("ops@acme.test",),
    file_link="https://drive.google.com/file/d/1figma/view",
    inr_rate=Decimal("83.5"),
)
SLACK = SummaryRow(
    vendor="Slack",
    document_type="receipt",
    invoice_date=date(2026, 8, 3),
    total=Decimal("8.75"),
    currency="USD",
    source_accounts=("finance@acme.test", "ops@acme.test"),
    file_link="https://drive.google.com/file/d/1slack/view",
    inr_rate=Decimal("83.0"),
    notes="found in two source accounts",
)


def examined(
    message_id: str,
    state: EmailState,
    subject: str,
    *,
    source_account: str = "ops@acme.test",
    reason: str | None = None,
    invoice_format: InvoiceFormat | None = None,
    portal_link: str | None = None,
) -> ExaminedEmail:
    return ExaminedEmail(
        source_account, message_id, subject, state, reason, invoice_format, portal_link
    )


REPORT = MonthReport(
    examined_emails=[
        examined("m-figma", EmailState.COLLECTED, "Your Figma invoice"),
        examined(
            "m-aws",
            EmailState.NEEDS_REVIEW,
            "AWS billing statement",
            reason="manual download needed",
            invoice_format=InvoiceFormat.PORTAL_LINK,
            portal_link="https://console.aws.amazon.com/billing",
        ),
        examined(
            "m-news",
            EmailState.SKIPPED,
            "=Our newsletter",
            source_account="finance@acme.test",
            reason="not a billing email",
        ),
        examined("m-zoom", EmailState.FAILED, "Zoom receipt", reason="portal did not answer"),
    ],
    billing_signals=[
        BillingSignal(
            kind="payment_failed",
            vendor="Notion",
            source_account="finance@acme.test",
            message_id="m-notion",
            subject="Your payment failed",
            received_at=datetime(2026, 8, 12, 9, 30, tzinfo=UTC),
        )
    ],
)


class Google:
    def __init__(self) -> None:
        self.drive = FakeDrive()
        self.sheets = FakeSheets(self.drive)

    def summary(self, report: MonthReport = REPORT, **kwargs: str) -> SheetSummary:
        return SheetSummary(self.sheets, self.drive, AUGUST, report, **kwargs)

    def spreadsheet(self) -> str:
        [found] = [f for f in self.drive.files_by_id.values() if f.mime_type == SPREADSHEET]
        return found.id

    def rows(self, tab: str) -> list[list[object]]:
        return self.sheets.tab(self.spreadsheet(), tab).rows


def test_sheet_is_a_summary_writer() -> None:
    writer: SummaryWriter = Google().summary()

    assert writer


def test_spreadsheet_for_the_collection_month_is_created_in_the_root_folder() -> None:
    google = Google()

    google.summary().write([FIGMA])

    [root] = [f for f in google.drive.named("Invoice Collection") if f.mime_type == FOLDER]
    [sheet] = google.drive.named("Invoice summary 2026-08")
    assert (sheet.mime_type, sheet.parent) == (SPREADSHEET, root.id)


def test_rerunning_the_month_reuses_its_spreadsheet() -> None:
    google = Google()
    google.summary().write([FIGMA])

    google.summary().write([FIGMA, SLACK])

    assert len(google.drive.named("Invoice summary 2026-08")) == 1


def test_spreadsheet_holds_exactly_the_four_tabs() -> None:
    google = Google()

    google.summary().write([FIGMA])

    assert [t.title for t in google.sheets.tabs(google.spreadsheet())] == TABS


def test_summary_tab_has_the_columns_of_the_csv_and_a_rupee_total() -> None:
    google = Google()

    google.summary().write([SLACK, FIGMA])

    header, slack, figma, total = google.rows("Summary")
    assert header == list(COLUMNS)
    assert slack == [
        "Slack",
        "2026-08-03",
        8.75,
        "USD",
        "finance@acme.test; ops@acme.test",
        "https://drive.google.com/file/d/1slack/view",
        "receipt",
        726.25,
        83.0,
        "found in two source accounts",
    ]
    assert figma[0] == "Figma"
    assert total[0] == "Total"
    assert total[COLUMNS.index("amount_inr")] == 726.25 + 15865.0


def test_rupee_total_says_how_many_rows_it_leaves_out() -> None:
    google = Google()
    no_rate = replace(FIGMA, inr_rate=None)

    google.summary().write([SLACK, no_rate])

    *_, no_rate_row, total = google.rows("Summary")
    assert no_rate_row[COLUMNS.index("amount_inr")] == ""
    assert total[COLUMNS.index("amount_inr")] == 726.25
    assert total[COLUMNS.index("notes")] == "leaves out 1 row with no rupee amount"


def test_pending_review_tab_links_each_email_into_the_dashboard() -> None:
    google = Google()

    google.summary(dashboard_url="https://dash.acme.test/").write([FIGMA])

    assert google.rows("Pending review") == [
        ["source_account", "subject", "reason", "portal_link", "dashboard_link"],
        [
            "ops@acme.test",
            "AWS billing statement",
            "manual download needed",
            "https://console.aws.amazon.com/billing",
            "https://dash.acme.test/review?month=2026-08&email=m-aws",
        ],
    ]


def test_dashboard_link_points_at_the_local_dashboard_by_default() -> None:
    google = Google()

    google.summary().write([FIGMA])

    assert google.rows("Pending review")[1][-1] == (
        "http://localhost:5173/review?month=2026-08&email=m-aws"
    )


def test_skipped_and_failed_tab_gives_each_email_with_its_reason() -> None:
    google = Google()

    google.summary().write([FIGMA])

    assert google.rows("Skipped and failed") == [
        ["state", "source_account", "subject", "reason"],
        ["skipped", "finance@acme.test", "'=Our newsletter", "not a billing email"],
        ["failed", "ops@acme.test", "Zoom receipt", "portal did not answer"],
    ]


def test_billing_signals_tab_lists_each_billing_signal() -> None:
    google = Google()

    google.summary().write([FIGMA])

    assert google.rows("Billing signals") == [
        ["kind", "vendor", "source_account", "subject", "date"],
        ["payment_failed", "Notion", "finance@acme.test", "Your payment failed", "2026-08-12"],
    ]


def test_vendor_name_that_looks_like_a_formula_is_written_as_text() -> None:
    google = Google()
    formula = replace(FIGMA, vendor='=HYPERLINK("http://evil.test")')

    google.summary().write([formula])

    assert google.rows("Summary")[1][0] == '\'=HYPERLINK("http://evil.test")'
    assert set(google.sheets.value_input_options) == {"RAW"}


def test_rerunning_with_a_shorter_summary_leaves_no_stale_rows() -> None:
    google = Google()
    google.summary().write([FIGMA, SLACK])

    google.summary(MonthReport(examined_emails=[], billing_signals=[])).write([SLACK])

    assert [row[0] for row in google.rows("Summary")] == ["vendor", "Slack", "Total"]
    assert google.rows("Pending review") == [
        ["source_account", "subject", "reason", "portal_link", "dashboard_link"]
    ]
    assert len(google.rows("Billing signals")) == 1


def test_header_row_of_every_tab_is_frozen_and_bold() -> None:
    google = Google()

    google.summary().write([FIGMA])

    tabs = google.sheets.tabs(google.spreadsheet())
    assert [(t.frozen_rows, t.bold_rows) for t in tabs] == [(1, {0})] * 4


def test_month_report_reads_the_examined_emails_and_billing_signals_of_the_month(
    tmp_path: Path,
) -> None:
    ledger = Ledger(tmp_path / "ledger.sqlite")
    email = Email(
        source_account="finance@acme.test",
        message_id="m-notion",
        sender="Notion <billing@notion.so>",
        subject="Your payment failed",
        received_at=datetime(2026, 8, 12, 9, 30, tzinfo=UTC),
    )
    signal = BillingSignal(
        "payment_failed", "Notion", email.source_account, email.message_id, email.subject,
        email.received_at,
    )  # fmt: skip
    ledger.record(AUGUST, email, EmailState.SKIPPED, reason="billing signal", signal=signal)

    report = month_report(ledger, AUGUST)
    ledger.close()

    assert [e.message_id for e in report.examined_emails] == ["m-notion"]
    assert report.billing_signals == [signal]


def test_skipped_and_failed_tab_names_each_source_account_that_could_not_be_read() -> None:
    google = Google()
    report = replace(
        REPORT,
        unread_source_accounts=[Sync("design@acme.test", False, "the sign-in has expired")],
    )

    google.summary(report).write([FIGMA])

    rows = google.rows("Skipped and failed")
    assert rows[1] == ["not read", "design@acme.test", "", "the sign-in has expired"]
    assert len(rows) == 4


def test_month_report_reads_the_source_accounts_that_could_not_be_read(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.sqlite")
    ledger.record_sync(AUGUST, "ops@acme.test")
    ledger.record_sync(AUGUST, "design@acme.test", reason="the sign-in has expired")

    report = month_report(ledger, AUGUST)
    ledger.close()

    assert report.unread_source_accounts == [
        Sync("design@acme.test", False, "the sign-in has expired")
    ]
