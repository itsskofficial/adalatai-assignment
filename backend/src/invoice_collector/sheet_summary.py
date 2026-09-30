"""The summary as a Google Sheet, one spreadsheet per collection month. See ADR 0006.

The sheet is a read-only report: every run rewrites its tabs from the ledger, and the
dashboard is where people act on what it shows.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from itertools import groupby
from typing import Any, cast
from urllib.parse import urlencode

from invoice_collector.api.settings import DEFAULT_PUBLIC_URL
from invoice_collector.domain import BillingSignal, CollectionMonth, EmailState, SummaryRow, Sync
from invoice_collector.drive_archive import DEFAULT_ROOT_FOLDER, DriveFolders
from invoice_collector.ledger import ExaminedEmail, Ledger
from invoice_collector.naming import filename
from invoice_collector.summary import COLUMNS, as_text_cell

SPREADSHEET = "application/vnd.google-apps.spreadsheet"
# Where people open the dashboard unless INVOICE_COLLECTOR_PUBLIC_URL says otherwise.
DEFAULT_DASHBOARD_URL = DEFAULT_PUBLIC_URL
_RETRIES = 3

Cell = str | float
Rows = list[list[Cell]]


@dataclass(frozen=True)
class MonthReport:
    """What the sheet shows beside the summary, for one collection month."""

    examined_emails: Sequence[ExaminedEmail]
    billing_signals: Sequence[BillingSignal]
    # Source accounts whose mail could not be read for the month, with the reason.
    unread_source_accounts: Sequence[Sync] = field(default_factory=list[Sync])


def month_report(ledger: Ledger, month: CollectionMonth) -> MonthReport:
    return MonthReport(
        ledger.examined_emails(month),
        ledger.billing_signals(month),
        [sync for sync in ledger.syncs(month) if not sync.succeeded],
    )


SKIPPED_AND_FAILED_COLUMNS = ("state", "source_account", "subject", "reason")


def skipped_and_failed(report: MonthReport) -> Rows:
    """Each source account that could not be read, then each email skipped or failed, with
    the reason. The sheet's tab of that name, and the CSV written beside the summary."""
    table: Rows = [list(SKIPPED_AND_FAILED_COLUMNS)]
    for sync in report.unread_source_accounts:
        table.append(
            ["not read", as_text_cell(sync.source_account), "", as_text_cell(sync.reason or "")]
        )
    for email in report.examined_emails:
        if email.state in (EmailState.SKIPPED, EmailState.FAILED):
            table.append(
                [
                    email.state.value,
                    as_text_cell(email.source_account),
                    as_text_cell(email.subject),
                    as_text_cell(email.reason or ""),
                ]
            )
    return table


def spreadsheet_name(month: CollectionMonth) -> str:
    return f"Invoice summary {month}"


# The header row each tab shows. The CSV files keep their column keys.
HEADINGS: dict[str, tuple[str, ...]] = {
    "Summary": (
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
    ),
    "Pending review": ("Source account", "Subject", "Reason", "Portal link", "Dashboard link"),
    "Skipped and failed": ("State", "Source account", "Subject", "Reason"),
    "Billing signals": ("Kind", "Vendor", "Source account", "Subject", "Date received"),
}
# Columns of long text, held to one width and wrapped rather than widened to fit.
WRAPPED = ("Subject", "Reason", "Portal link", "Notes")
# How wide each column is, in pixels. Set rather than fitted to the text: Sheets fits a
# column to its values and leaves the heading, with its filter button, clipped.
WIDTHS = {
    "Vendor": 140,
    "Invoice date": 120,
    "Amount": 110,
    "Currency": 100,
    "Source account": 400,
    "File": 280,
    "Document type": 140,
    "Amount in rupees": 160,
    "Rate to rupees": 140,
    "State": 130,
    "Kind": 150,
    "Date received": 140,
    "Dashboard link": 230,
}
WRAPPED_WIDTH = 360
REVIEW_LINK_TEXT = "Open on the Review screen"
AMOUNT_PATTERN = "#,##0.00"
RATE_PATTERN = "#,##0.0000"
DATE_PATTERN = "yyyy-mm-dd"
# Sheets counts dates as days since this one.
_SERIAL_EPOCH = date(1899, 12, 30)


def _rgb(red: float, green: float, blue: float) -> dict[str, Any]:
    return {"rgbColor": {"red": red, "green": green, "blue": blue}}


TAB_COLOURS = {
    "Summary": _rgb(0.20, 0.66, 0.33),
    "Pending review": _rgb(1.0, 0.75, 0.0),
    "Skipped and failed": _rgb(0.86, 0.20, 0.18),
    "Billing signals": _rgb(0.60, 0.60, 0.60),
}
_HEADER_GREY = _rgb(0.90, 0.90, 0.90)
_BAND_GREY = _rgb(0.94, 0.94, 0.94)
_WHITE = _rgb(1.0, 1.0, 1.0)
_CREDIT_RED = _rgb(0.80, 0.0, 0.0)


def rupee_pattern(amount: Decimal | None) -> str:
    """The number format that groups a rupee amount the Indian way, as 4,49,273.20.

    One section with the commas written in, chosen for the amount's number of digits:
    a format with conditions ([>=100000]...) has room for two, so it cannot cover both
    crores and a credit note's negative amount, and its sign in the last section is not
    certain. A single section keeps the minus sign. Below a lakh the groupings agree.
    """
    digits = len(str(abs(int(amount)))) if amount is not None else 0
    if digits <= 5:
        return AMOUNT_PATTERN
    return "##\\," * ((digits - 2) // 2) + "##0.00"


def serial_date(day: date) -> int:
    """A date as the number Sheets keeps for it, so the cell is a date and not text."""
    return (day - _SERIAL_EPOCH).days


def _is_web_link(file_link: str) -> bool:
    return file_link.startswith(("https://", "http://"))


def _file_text(row: SummaryRow) -> str:
    """What the File column shows: for a link into Drive, the name the run gave the file,
    as the link itself names nothing; a path on this machine is shown whole."""
    return as_text_cell(filename(row) if _is_web_link(row.file_link) else row.file_link)


def _number(value: Decimal | None) -> Cell:
    return float(value) if value is not None else ""


def _summary_rows(rows: Sequence[SummaryRow]) -> Rows:
    table: Rows = [list(HEADINGS["Summary"])]
    for row in rows:
        table.append(
            [
                as_text_cell(row.vendor),
                serial_date(row.invoice_date),
                _number(row.total),
                as_text_cell(row.currency),
                as_text_cell(row.source_account),
                _file_text(row),
                row.document_type,
                _number(row.inr_total),
                _number(row.inr_rate),
                as_text_cell(row.notes),
            ]
        )
    in_rupees = [row.inr_total for row in rows if row.inr_total is not None]
    left_out = len(rows) - len(in_rupees)
    total: list[Cell] = [""] * len(COLUMNS)
    total[0] = "Total"
    total[COLUMNS.index("amount_inr")] = float(sum(in_rupees, Decimal(0)))
    if left_out:
        total[COLUMNS.index("notes")] = (
            f"leaves out {left_out} row{'s' if left_out > 1 else ''} with no rupee amount"
        )
    table.append(total)
    return table


class SheetSummary:
    """Writes the summary and its companion tabs to the collection month's spreadsheet.

    sheets and drive are Sheets v4 and Drive v3 clients acting as the owner account.
    The spreadsheet is created through Drive, in the root folder, so the drive.file
    scope covers it. Re-running a month rewrites the same spreadsheet in place.
    """

    TABS = ("Summary", "Pending review", "Skipped and failed", "Billing signals")

    def __init__(
        self,
        sheets: Any,
        drive: Any,
        month: CollectionMonth,
        report: MonthReport,
        *,
        root_folder: str = DEFAULT_ROOT_FOLDER,
        dashboard_url: str = DEFAULT_DASHBOARD_URL,
    ) -> None:
        self._sheets = sheets
        self._folders = DriveFolders(drive, root_folder)
        self._month = month
        self._report = report
        self._dashboard_url = dashboard_url.rstrip("/")

    def write(self, rows: Sequence[SummaryRow]) -> None:
        spreadsheet_id = self._spreadsheet_id()
        tab_ids, bandings = self._tabs(spreadsheet_id)
        skipped = skipped_and_failed(self._report)
        contents = dict(
            zip(
                self.TABS,
                (
                    _summary_rows(rows),
                    self._pending(),
                    [list(HEADINGS["Skipped and failed"]), *skipped[1:]],
                    self._signals(),
                ),
                strict=True,
            )
        )
        values = self._sheets.spreadsheets().values()
        values.batchClear(
            spreadsheetId=spreadsheet_id, body={"ranges": [_range(t) for t in self.TABS]}
        ).execute(num_retries=_RETRIES)
        values.batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "valueInputOption": "RAW",
                "data": [
                    {"range": f"{_range(tab)}!A1", "values": table}
                    for tab, table in contents.items()
                ],
            },
        ).execute(num_retries=_RETRIES)
        # Bandings are removed and added again, as a second one over the same rows is
        # refused; every other request sets the same formats however often it is sent.
        requests: list[dict[str, Any]] = [
            {"deleteBanding": {"bandedRangeId": banding}} for banding in bandings
        ]
        for index, tab in enumerate(self.TABS):
            sheet_id = tab_ids[tab]
            table = contents[tab]
            # The Summary's last row is its total, kept out of the filter and the bands.
            data_end = len(table) - 1 if tab == "Summary" else len(table)
            requests += _layout(sheet_id, index, tab, data_end)
            if tab == "Summary":
                requests += _summary_formats(sheet_id, rows)
            elif tab == "Pending review":
                requests += self._review_links(sheet_id)
            elif tab == "Billing signals":
                requests += _dates(sheet_id, tab, "Date received", data_end)
            requests += _widths(sheet_id, tab)
        self._batch_update(spreadsheet_id, requests)

    def _spreadsheet_id(self) -> str:
        name = spreadsheet_name(self._month)
        root = self._folders.folder_id()
        existing = self._folders.find(name, root, SPREADSHEET)
        if existing:
            return str(existing[0]["id"])
        return str(self._folders.create(name, root, SPREADSHEET)["id"])

    def _tabs(self, spreadsheet_id: str) -> tuple[dict[str, int], list[int]]:
        """Makes the spreadsheet hold exactly the four tabs, and gives their ids and the
        ids of the bandings an earlier run left on them."""
        answer: dict[str, Any] = (
            self._sheets.spreadsheets()
            .get(
                spreadsheetId=spreadsheet_id,
                fields="sheets(properties(sheetId,title),bandedRanges(bandedRangeId))",
            )
            .execute(num_retries=_RETRIES)
        )
        present = {
            str(s["properties"]["title"]): int(s["properties"]["sheetId"])
            for s in answer.get("sheets", [])
        }
        # A tab that is deleted takes its bandings with it.
        bandings = [
            int(banded["bandedRangeId"])
            for s in answer.get("sheets", [])
            if s["properties"]["title"] in self.TABS
            for banded in s.get("bandedRanges", [])
        ]
        # New tabs are added before others are removed, as a spreadsheet needs one tab.
        requests: list[dict[str, Any]] = [
            {"addSheet": {"properties": {"title": tab}}} for tab in self.TABS if tab not in present
        ] + [
            {"deleteSheet": {"sheetId": sheet_id}}
            for title, sheet_id in present.items()
            if title not in self.TABS
        ]
        if requests:
            for reply in self._batch_update(spreadsheet_id, requests):
                if "addSheet" in reply:
                    added = reply["addSheet"]["properties"]
                    present[str(added["title"])] = int(added["sheetId"])
        return {tab: present[tab] for tab in self.TABS}, bandings

    def _batch_update(
        self, spreadsheet_id: str, requests: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        answer: dict[str, Any] = (
            self._sheets.spreadsheets()
            .batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": requests})
            .execute(num_retries=_RETRIES)
        )
        return list(answer.get("replies", []))

    def _emails_in(self, *states: EmailState) -> list[ExaminedEmail]:
        return [e for e in self._report.examined_emails if e.state in states]

    def _dashboard_link(self, email: ExaminedEmail) -> str:
        query = urlencode({"month": str(self._month), "email": email.message_id})
        return f"{self._dashboard_url}/review?{query}"

    def _pending(self) -> Rows:
        table: Rows = [list(HEADINGS["Pending review"])]
        for email in self._emails_in(EmailState.NEEDS_REVIEW):
            table.append(
                [
                    as_text_cell(email.source_account),
                    as_text_cell(email.subject),
                    as_text_cell(email.reason or ""),
                    # Left as text: it was read from the email, which is not trusted.
                    as_text_cell(email.portal_link or ""),
                    REVIEW_LINK_TEXT,
                ]
            )
        return table

    def _review_links(self, sheet_id: int) -> list[dict[str, Any]]:
        """The Pending review tab's dashboard column, each cell a link to the email."""
        emails = self._emails_in(EmailState.NEEDS_REVIEW)
        column = HEADINGS["Pending review"].index("Dashboard link")
        cells = [_link_cell(REVIEW_LINK_TEXT, self._dashboard_link(e)) for e in emails]
        return _column_cells(sheet_id, column, cells)

    def _signals(self) -> Rows:
        table: Rows = [list(HEADINGS["Billing signals"])]
        for signal in self._report.billing_signals:
            table.append(
                [
                    signal.kind,
                    as_text_cell(signal.vendor or ""),
                    as_text_cell(signal.source_account),
                    as_text_cell(signal.subject),
                    serial_date(signal.received_at.date()),
                ]
            )
        return table


def _range(tab: str) -> str:
    """The whole of a tab, in the A1 notation of the Sheets API."""
    return "'{}'".format(tab.replace("'", "''"))


def _grid(
    sheet_id: int, rows: tuple[int, int] | None = None, columns: tuple[int, int] | None = None
) -> dict[str, Any]:
    """A range of a tab: whole rows or columns where the other bound is not given."""
    grid: dict[str, Any] = {"sheetId": sheet_id}
    if rows is not None:
        grid |= {"startRowIndex": rows[0], "endRowIndex": rows[1]}
    if columns is not None:
        grid |= {"startColumnIndex": columns[0], "endColumnIndex": columns[1]}
    return grid


def _format_fields(fmt: dict[str, Any], prefix: str = "userEnteredFormat") -> list[str]:
    """The field mask naming just the formats given, so one format leaves the others be."""
    fields: list[str] = []
    for key, value in fmt.items():
        path = f"{prefix}.{key}"
        if isinstance(value, dict) and key != "numberFormat" and not key.endswith("Style"):
            fields += _format_fields(cast(dict[str, Any], value), path)
        else:
            fields.append(path)
    return fields


def _format(grid: dict[str, Any], fmt: dict[str, Any], fields: str = "") -> dict[str, Any]:
    return {
        "repeatCell": {
            "range": grid,
            "cell": {"userEnteredFormat": fmt},
            "fields": fields or ",".join(_format_fields(fmt)),
        }
    }


def _number_format(pattern: str) -> dict[str, Any]:
    return {"numberFormat": {"type": "NUMBER", "pattern": pattern}, "horizontalAlignment": "RIGHT"}


def _link_cell(text: str, url: str) -> dict[str, Any]:
    """Text that opens a link, with no HYPERLINK formula: the link rides on the text."""
    link = {"link": {"uri": url}, "underline": True, "foregroundColorStyle": _rgb(0.07, 0.33, 0.8)}
    return {
        "userEnteredValue": {"stringValue": text},
        "textFormatRuns": [{"startIndex": 0, "format": link}],
    }


def _column_cells(sheet_id: int, column: int, cells: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Writes one cell per data row down a column, below the header."""
    if not cells:
        return []
    return [
        {
            "updateCells": {
                "range": _grid(sheet_id, (1, 1 + len(cells)), (column, column + 1)),
                "rows": [{"values": [cell]} for cell in cells],
                "fields": "userEnteredValue,textFormatRuns",
            }
        }
    ]


def _layout(sheet_id: int, index: int, tab: str, data_end: int) -> list[dict[str, Any]]:
    """What every tab has: its place and colour, a bold grey frozen header row with a
    filter, bands on the data rows (1 to data_end), and long text wrapped."""
    headings = HEADINGS[tab]
    width = len(headings)
    requests: list[dict[str, Any]] = [
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sheet_id,
                    "index": index,
                    "tabColorStyle": TAB_COLOURS[tab],
                    "gridProperties": {"frozenRowCount": 1},
                },
                "fields": "index,tabColorStyle,gridProperties.frozenRowCount",
            }
        },
        # Clears the formats of an earlier run, whose rows may have held other things.
        _format(_grid(sheet_id), {"verticalAlignment": "TOP"}, fields="userEnteredFormat"),
        _format(
            _grid(sheet_id, (0, 1)),
            {"textFormat": {"bold": True}, "backgroundColorStyle": _HEADER_GREY},
        ),
        {"setBasicFilter": {"filter": {"range": _grid(sheet_id, (0, data_end), (0, width))}}},
    ]
    if data_end > 1:
        requests.append(
            {
                "addBanding": {
                    "bandedRange": {
                        "range": _grid(sheet_id, (1, data_end), (0, width)),
                        "rowProperties": {
                            "firstBandColorStyle": _WHITE,
                            "secondBandColorStyle": _BAND_GREY,
                        },
                    }
                }
            }
        )
    requests += [
        _format(_grid(sheet_id, None, (column, column + 1)), {"wrapStrategy": "WRAP"})
        for column, heading in enumerate(headings)
        if heading in WRAPPED
    ]
    return requests


def _widths(sheet_id: int, tab: str) -> list[dict[str, Any]]:
    """Each column at its set width; long text at WRAPPED_WIDTH, and wrapped."""
    headings = HEADINGS[tab]

    def columns(start: int, end: int) -> dict[str, Any]:
        return {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": start, "endIndex": end}

    return [
        {
            "updateDimensionProperties": {
                "range": columns(column, column + 1),
                "properties": {
                    "pixelSize": WRAPPED_WIDTH if heading in WRAPPED else WIDTHS[heading]
                },
                "fields": "pixelSize",
            }
        }
        for column, heading in enumerate(headings)
    ]


def _dates(sheet_id: int, tab: str, heading: str, data_end: int) -> list[dict[str, Any]]:
    if data_end <= 1:
        return []
    column = HEADINGS[tab].index(heading)
    return [
        _format(
            _grid(sheet_id, (1, data_end), (column, column + 1)),
            {"numberFormat": {"type": "DATE", "pattern": DATE_PATTERN}},
        )
    ]


def _summary_formats(sheet_id: int, rows: Sequence[SummaryRow]) -> list[dict[str, Any]]:
    """Dates as dates, amounts as numbers, rupees grouped the Indian way, credit notes in
    red, a bold total under a rule, and each file's name a link to it."""
    headings = HEADINGS["Summary"]
    width = len(headings)
    amount, rupees = headings.index("Amount"), headings.index("Amount in rupees")
    rate = headings.index("Rate to rupees")
    total_row = 1 + len(rows)

    def cells(top: int, bottom: int, column: int) -> dict[str, Any]:
        return _grid(sheet_id, (top, bottom), (column, column + 1))

    requests = _dates(sheet_id, "Summary", "Invoice date", total_row)
    if rows:
        requests += [
            _format(cells(1, total_row, amount), _number_format(AMOUNT_PATTERN)),
            _format(cells(1, total_row, rate), _number_format(RATE_PATTERN)),
        ]
    # Each rupee amount, the total with them, in the format for its number of digits.
    in_rupees = [row.inr_total for row in rows]
    in_rupees.append(sum((r.inr_total for r in rows if r.inr_total is not None), Decimal(0)))
    patterns = [rupee_pattern(value) for value in in_rupees]
    top = 1
    for pattern, run in groupby(patterns):
        bottom = top + len(list(run))
        requests.append(_format(cells(top, bottom, rupees), _number_format(pattern)))
        top = bottom
    red = {"textFormat": {"foregroundColorStyle": _CREDIT_RED}}
    requests += [
        _format(cells(line, line + 1, column), red)
        for line, row in enumerate(rows, start=1)
        if row.document_type == "credit_note"
        for column in (amount, rupees)
    ]
    total = _grid(sheet_id, (total_row, total_row + 1), (0, width))
    requests += [
        _format(total, {"textFormat": {"bold": True}}),
        {"updateBorders": {"range": total, "top": {"style": "SOLID", "colorStyle": _rgb(0, 0, 0)}}},
    ]
    files = [
        _link_cell(_file_text(row), row.file_link)
        if _is_web_link(row.file_link)
        else {"userEnteredValue": {"stringValue": _file_text(row)}}
        for row in rows
    ]
    return requests + _column_cells(sheet_id, headings.index("File"), files)
