"""The summary as a Google Sheet, one spreadsheet per collection month. See ADR 0006.

The sheet is a read-only report: every run rewrites its tabs from the ledger, and the
dashboard is where people act on what it shows.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

from invoice_collector.domain import BillingSignal, CollectionMonth, EmailState, SummaryRow, Sync
from invoice_collector.drive_archive import DEFAULT_ROOT_FOLDER, DriveFolders
from invoice_collector.ledger import ExaminedEmail, Ledger
from invoice_collector.summary import COLUMNS, as_text_cell

SPREADSHEET = "application/vnd.google-apps.spreadsheet"
DEFAULT_DASHBOARD_URL = "http://localhost:5173"
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


def _number(value: Decimal | None) -> Cell:
    return float(value) if value is not None else ""


def _summary_rows(rows: Sequence[SummaryRow]) -> Rows:
    table: Rows = [list(COLUMNS)]
    for row in rows:
        table.append(
            [
                as_text_cell(row.vendor),
                row.invoice_date.isoformat(),
                _number(row.total),
                as_text_cell(row.currency),
                as_text_cell(row.source_account),
                as_text_cell(row.file_link),
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
        tab_ids = self._tabs(spreadsheet_id)
        contents = dict(
            zip(
                self.TABS,
                (
                    _summary_rows(rows),
                    self._pending(),
                    skipped_and_failed(self._report),
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
        self._batch_update(
            spreadsheet_id, [r for tab in self.TABS for r in _header_format(tab_ids[tab])]
        )

    def _spreadsheet_id(self) -> str:
        name = spreadsheet_name(self._month)
        root = self._folders.folder_id()
        existing = self._folders.find(name, root, SPREADSHEET)
        if existing:
            return str(existing[0]["id"])
        return str(self._folders.create(name, root, SPREADSHEET)["id"])

    def _tabs(self, spreadsheet_id: str) -> dict[str, int]:
        """Makes the spreadsheet hold exactly the four tabs, and gives their ids."""
        answer: dict[str, Any] = (
            self._sheets.spreadsheets()
            .get(spreadsheetId=spreadsheet_id, fields="sheets.properties(sheetId,title)")
            .execute(num_retries=_RETRIES)
        )
        present = {
            str(s["properties"]["title"]): int(s["properties"]["sheetId"])
            for s in answer.get("sheets", [])
        }
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
        return {tab: present[tab] for tab in self.TABS}

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
        table: Rows = [["source_account", "subject", "reason", "portal_link", "dashboard_link"]]
        for email in self._emails_in(EmailState.NEEDS_REVIEW):
            table.append(
                [
                    as_text_cell(email.source_account),
                    as_text_cell(email.subject),
                    as_text_cell(email.reason or ""),
                    as_text_cell(email.portal_link or ""),
                    self._dashboard_link(email),
                ]
            )
        return table

    def _signals(self) -> Rows:
        table: Rows = [["kind", "vendor", "source_account", "subject", "date"]]
        for signal in self._report.billing_signals:
            table.append(
                [
                    signal.kind,
                    as_text_cell(signal.vendor or ""),
                    as_text_cell(signal.source_account),
                    as_text_cell(signal.subject),
                    signal.received_at.date().isoformat(),
                ]
            )
        return table


def _range(tab: str) -> str:
    """The whole of a tab, in the A1 notation of the Sheets API."""
    return "'{}'".format(tab.replace("'", "''"))


def _header_format(sheet_id: int) -> list[dict[str, Any]]:
    return [
        {
            "updateSheetProperties": {
                "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}},
                "fields": "gridProperties.frozenRowCount",
            }
        },
        {
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
                "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                "fields": "userEnteredFormat.textFormat.bold",
            }
        },
    ]
