"""The summary in a Google Sheet, checked against fake Drive and Sheets services."""

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fake_google import FOLDER, SPREADSHEET, FakeDrive, FakeSheets, Tab
from googleapiclient.errors import HttpError

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
from invoice_collector.sheet_summary import (
    HEADINGS,
    WIDTHS,
    MonthReport,
    SheetSummary,
    month_report,
    rupee_pattern,
    serial_date,
)
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

    def summary(self, report: MonthReport = REPORT, **kwargs: Any) -> SheetSummary:
        return SheetSummary(self.sheets, self.drive, AUGUST, report, **kwargs)

    def spreadsheet(self) -> str:
        [found] = [f for f in self.drive.files_by_id.values() if f.mime_type == SPREADSHEET]
        return found.id

    def rows(self, tab: str) -> list[list[object]]:
        return self.sheets.tab(self.spreadsheet(), tab).rows

    def tab(self, title: str) -> Tab:
        return self.sheets.tab(self.spreadsheet(), title)

    def formats(self, title: str, column: str) -> list[dict[str, Any]]:
        """Each repeatCell sent for the column of the tab: its rows and cell format."""
        sheet_id = self.tab(title).sheet_id
        index = HEADINGS[title].index(column)
        return [
            {
                "rows": (r["range"].get("startRowIndex"), r["range"].get("endRowIndex")),
                **r["cell"]["userEnteredFormat"],
            }
            for r in self.sheets.sent("repeatCell")
            if r["range"]["sheetId"] == sheet_id
            and r["range"].get("startColumnIndex") == index
            and r["range"].get("endColumnIndex") == index + 1
        ]


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


def test_summary_tab_has_the_columns_of_the_csv_under_names_and_a_rupee_total() -> None:
    google = Google()

    google.summary().write([SLACK, FIGMA])

    header, slack, figma, total = google.rows("Summary")
    assert header == [
        "Vendor",
        "Invoice date",
        "Amount",
        "Currency",
        "Source account",
        "File",
        "Document type",
        "Amount in rupees",
        "Rate to rupees",
        "Notes",
    ]
    assert len(header) == len(COLUMNS)
    assert slack == [
        "Slack",
        serial_date(date(2026, 8, 3)),
        8.75,
        "USD",
        "finance@acme.test; ops@acme.test",
        "2026-08_Slack_8.75-USD.pdf",
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
        ["Source account", "Subject", "Reason", "Portal link", "Dashboard link"],
        [
            "ops@acme.test",
            "AWS billing statement",
            "manual download needed",
            "https://console.aws.amazon.com/billing",
            "Open on the Review screen",
        ],
    ]
    # The link rides on the text; the portal link, read from the email, carries none.
    assert google.tab("Pending review").links == {
        (1, 4): "https://dash.acme.test/review?month=2026-08&email=m-aws"
    }


def test_dashboard_link_points_at_the_local_dashboard_by_default() -> None:
    google = Google()

    google.summary().write([FIGMA])

    assert google.tab("Pending review").links[(1, 4)] == (
        "http://localhost:8000/review?month=2026-08&email=m-aws"
    )


def test_skipped_and_failed_tab_gives_each_email_with_its_reason() -> None:
    google = Google()

    google.summary().write([FIGMA])

    assert google.rows("Skipped and failed") == [
        ["State", "Source account", "Subject", "Reason"],
        ["skipped", "finance@acme.test", "'=Our newsletter", "not a billing email"],
        ["failed", "ops@acme.test", "Zoom receipt", "portal did not answer"],
    ]


def test_billing_signals_tab_lists_each_billing_signal() -> None:
    google = Google()

    google.summary().write([FIGMA])

    assert google.rows("Billing signals") == [
        ["Kind", "Vendor", "Source account", "Subject", "Date received"],
        [
            "payment_failed",
            "Notion",
            "finance@acme.test",
            "Your payment failed",
            serial_date(date(2026, 8, 12)),
        ],
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

    assert [row[0] for row in google.rows("Summary")] == ["Vendor", "Slack", "Total"]
    assert google.rows("Pending review") == [list(HEADINGS["Pending review"])]
    assert len(google.rows("Billing signals")) == 1


def test_header_row_of_every_tab_is_frozen_and_bold_and_the_total_row_bold() -> None:
    google = Google()

    google.summary().write([FIGMA])

    tabs = google.sheets.tabs(google.spreadsheet())
    assert [(t.frozen_rows, t.bold_rows) for t in tabs] == [(1, {0, 2})] + [(1, {0})] * 3


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


GREY = {"rgbColor": {"red": 0.9, "green": 0.9, "blue": 0.9}}
CREDIT = replace(
    FIGMA,
    vendor="Atlassian",
    document_type="credit_note",
    total=Decimal("-5400.00"),
    inr_rate=Decimal("83.2"),
    file_link="https://drive.google.com/file/d/1atlassian/view",
)


def test_header_row_of_every_tab_is_grey_with_a_filter_over_what_the_tab_holds() -> None:
    google = Google()

    google.summary().write([SLACK, FIGMA])

    for title in TABS:
        tab = google.tab(title)
        [header] = [
            r
            for r in google.sheets.sent("repeatCell")
            if r["range"] == {"sheetId": tab.sheet_id, "startRowIndex": 0, "endRowIndex": 1}
        ]
        assert header["cell"]["userEnteredFormat"]["backgroundColorStyle"] == GREY
        assert tab.basic_filter == {
            "sheetId": tab.sheet_id,
            "startRowIndex": 0,
            # The Summary's total row is left out, so sorting cannot move it.
            "endRowIndex": len(tab.rows) - 1 if title == "Summary" else len(tab.rows),
            "startColumnIndex": 0,
            "endColumnIndex": len(HEADINGS[title]),
        }


def test_tabs_are_coloured_and_kept_in_order() -> None:
    google = Google()
    google.summary().write([FIGMA])
    tabs = google.sheets.tabs(google.spreadsheet())
    tabs.reverse()

    google.summary().write([FIGMA])

    assert [(t.title, t.colour) for t in google.sheets.tabs(google.spreadsheet())] == [
        ("Summary", {"rgbColor": {"red": 0.2, "green": 0.66, "blue": 0.33}}),
        ("Pending review", {"rgbColor": {"red": 1.0, "green": 0.75, "blue": 0.0}}),
        ("Skipped and failed", {"rgbColor": {"red": 0.86, "green": 0.2, "blue": 0.18}}),
        ("Billing signals", {"rgbColor": {"red": 0.6, "green": 0.6, "blue": 0.6}}),
    ]


def test_amounts_are_numbers_right_aligned_and_the_rate_has_four_decimals() -> None:
    google = Google()

    google.summary().write([SLACK, FIGMA])

    right = {"horizontalAlignment": "RIGHT"}
    assert google.formats("Summary", "Amount") == [
        {"rows": (1, 3), "numberFormat": {"type": "NUMBER", "pattern": "#,##0.00"}, **right}
    ]
    assert google.formats("Summary", "Rate to rupees") == [
        {"rows": (1, 3), "numberFormat": {"type": "NUMBER", "pattern": "#,##0.0000"}, **right}
    ]


def test_rupee_amounts_and_their_total_are_grouped_the_indian_way() -> None:
    google = Google()

    # Slack is 726.25 in rupees, the credit note -4,49,280.00; the total -4,48,553.75.
    google.summary().write([SLACK, CREDIT])

    lakhs = r"##\,##\,##0.00"
    assert [
        (f["rows"], f["numberFormat"]["pattern"])
        for f in google.formats("Summary", "Amount in rupees")
        if "numberFormat" in f
    ] == [((1, 2), "#,##0.00"), ((2, 4), lakhs)]


def test_rupee_pattern_writes_in_the_commas_for_the_number_of_digits() -> None:
    assert rupee_pattern(None) == "#,##0.00"
    assert rupee_pattern(Decimal("99999.99")) == "#,##0.00"
    # 4,49,273.20 and 12,34,567.00
    assert rupee_pattern(Decimal("449273.20")) == r"##\,##\,##0.00"
    assert rupee_pattern(Decimal("-1234567")) == r"##\,##\,##0.00"
    # 1,23,45,678.00
    assert rupee_pattern(Decimal("12345678")) == r"##\,##\,##\,##0.00"


def test_dates_are_written_as_dates() -> None:
    google = Google()

    google.summary().write([SLACK, FIGMA])

    date_format = {"numberFormat": {"type": "DATE", "pattern": "yyyy-mm-dd"}}
    assert google.formats("Summary", "Invoice date") == [{"rows": (1, 3), **date_format}]
    assert google.formats("Billing signals", "Date received") == [{"rows": (1, 2), **date_format}]
    assert serial_date(date(2026, 8, 3)) == 46237


def test_amounts_of_a_credit_note_are_red_and_the_total_row_sits_under_a_rule() -> None:
    google = Google()

    google.summary().write([SLACK, CREDIT])

    red = {
        "textFormat": {
            "foregroundColorStyle": {"rgbColor": {"red": 0.8, "green": 0.0, "blue": 0.0}}
        }
    }
    for column in ("Amount", "Amount in rupees"):
        assert [f for f in google.formats("Summary", column) if "textFormat" in f] == [
            {"rows": (2, 3), **red}
        ]
    [border] = google.sheets.sent("updateBorders")
    assert (border["range"]["startRowIndex"], border["range"]["endRowIndex"]) == (3, 4)
    assert border["top"]["style"] == "SOLID"


def test_file_column_shows_each_file_by_name_and_opens_it_in_drive() -> None:
    google = Google()
    on_this_machine = replace(SLACK, file_link="/data/archive/2026-08/2026-08_Slack_8.75-USD.pdf")

    google.summary().write([FIGMA, on_this_machine])

    rows = google.rows("Summary")
    assert [row[5] for row in rows[1:3]] == [
        "2026-08_Figma_190.00-USD.pdf",
        "/data/archive/2026-08/2026-08_Slack_8.75-USD.pdf",
    ]
    assert google.tab("Summary").links == {(1, 5): "https://drive.google.com/file/d/1figma/view"}


def test_every_column_has_its_width_and_long_text_wraps() -> None:
    google = Google()

    google.summary().write([FIGMA])

    widths = {
        (r["range"]["sheetId"], r["range"]["startIndex"]): r["properties"]["pixelSize"]
        for r in google.sheets.sent("updateDimensionProperties")
    }
    # Every column of every tab is given a width; the headings are never clipped.
    assert len(widths) == sum(len(HEADINGS[title]) for title in TABS)
    summary = google.tab("Summary").sheet_id
    assert widths[(summary, 0)] == WIDTHS["Vendor"]
    assert widths[(summary, 7)] == WIDTHS["Amount in rupees"]
    pending = google.tab("Pending review").sheet_id
    assert widths[(pending, 1)] == widths[(pending, 2)] == 360
    assert widths[(summary, 9)] == 360
    assert google.formats("Skipped and failed", "Reason") == [
        {"rows": (None, None), "wrapStrategy": "WRAP"}
    ]


def test_rerunning_the_month_replaces_the_bands_and_sends_the_same_formats() -> None:
    google = Google()
    google.summary().write([SLACK, FIGMA])
    first = list(google.sheets.requests)
    google.sheets.requests.clear()

    google.summary().write([SLACK, FIGMA])

    # The first run also made the tabs; a rerun only takes away the bands it added.
    made = [r for r in first if "addSheet" in r or "deleteSheet" in r]
    assert [r for r in google.sheets.requests if "deleteBanding" not in r] == first[len(made) :]
    assert len(google.sheets.sent("deleteBanding")) == 4
    assert [len(google.tab(title).bandings) for title in TABS] == [1, 1, 1, 1]


def test_bands_are_put_right_when_the_answer_to_adding_them_is_lost() -> None:
    google = Google()
    google.summary().write([SLACK, FIGMA])
    # Sheets applies the rerun's bands, but the answer never arrives.
    google.sheets.lose_answer_to = ("addBanding", 503)
    waited: list[float] = []

    google.summary(sleep=waited.append).write([SLACK, FIGMA])

    # One wait before the second attempt, as long as the client would have made.
    assert len(waited) == 1 and 1 <= waited[0] < 2

    # Read again before the second attempt: the bands just added are the ones replaced,
    # so none stack and no id that is gone is asked for.
    assert [len(google.tab(title).bandings) for title in TABS] == [1, 1, 1, 1]
    assert len(google.sheets.sent("deleteBanding")) == 8
    assert len(google.sheets.sent("addBanding")) == 12


def test_a_refusal_of_the_bands_is_raised_not_tried_again() -> None:
    google = Google()
    google.sheets.lose_answer_to = ("addBanding", 400)

    with pytest.raises(HttpError):
        google.summary().write([SLACK, FIGMA])

    assert len(google.sheets.sent("addBanding")) == 4


def test_a_tab_with_no_rows_has_no_bands() -> None:
    google = Google()

    google.summary(MonthReport(examined_emails=[], billing_signals=[])).write([FIGMA])

    assert [len(google.tab(title).bandings) for title in TABS] == [1, 0, 0, 0]
