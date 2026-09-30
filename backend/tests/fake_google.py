"""In-memory stand-ins for the Google Drive and Sheets services, recording what was asked.

They answer the same chained calls as the real clients, such as
service.files().list(q=...).execute(), for the small part of each API the tool uses.
"""

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

FOLDER = "application/vnd.google-apps.folder"
SPREADSHEET = "application/vnd.google-apps.spreadsheet"

_NAME = re.compile(r"name = '((?:[^'\\]|\\.)*)'")
_PARENT = re.compile(r"'((?:[^'\\]|\\.)*)' in parents")
_MIME_TYPE = re.compile(r"mimeType = '([^']*)'")


def _unescape(text: str) -> str:
    return re.sub(r"\\(.)", r"\1", text)


class Request:
    def __init__(self, answer: Callable[[], dict[str, Any]]) -> None:
        self._answer = answer

    def execute(self, num_retries: int = 0) -> dict[str, Any]:
        return self._answer()


@dataclass
class DriveFile:
    id: str
    name: str
    mime_type: str
    parent: str
    content: bytes = b""
    trashed: bool = False

    @property
    def link(self) -> str:
        return f"https://drive.google.com/file/d/{self.id}/view"

    def as_answer(self) -> dict[str, Any]:
        answer: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "mimeType": self.mime_type,
            "parents": [self.parent],
            "webViewLink": self.link,
        }
        if self.mime_type not in (FOLDER, SPREADSHEET):
            answer["md5Checksum"] = hashlib.md5(self.content, usedforsecurity=False).hexdigest()
        return answer


# Named here because, inside FakeDrive, list is its method of that name.
DriveFiles = list[DriveFile]


class FakeDrive:
    """Drive v3, holding files in memory. The top of My Drive has the id 'root'."""

    def __init__(self) -> None:
        self.files_by_id: dict[str, DriveFile] = {}
        self.queries: list[str] = []
        self.created: list[DriveFile] = []

    def files(self) -> "FakeDrive":
        return self

    def add(self, name: str, mime_type: str, parent: str, content: bytes = b"") -> DriveFile:
        file = DriveFile(f"file-{len(self.files_by_id) + 1}", name, mime_type, parent, content)
        self.files_by_id[file.id] = file
        return file

    def list(self, q: str, **_: Any) -> Request:
        self.queries.append(q)
        name, parent, mime_type = _NAME.search(q), _PARENT.search(q), _MIME_TYPE.search(q)

        def matches(file: DriveFile) -> bool:
            return (
                not (file.trashed and "trashed = false" in q)
                and (name is None or file.name == _unescape(name.group(1)))
                and (parent is None or file.parent == _unescape(parent.group(1)))
                and (mime_type is None or file.mime_type == mime_type.group(1))
            )

        found = [f.as_answer() for f in self.files_by_id.values() if matches(f)]
        return Request(lambda: {"files": found})

    def create(self, body: dict[str, Any], media_body: Any = None, **_: Any) -> Request:
        content = b"" if media_body is None else bytes(media_body.getbytes(0, media_body.size()))
        [parent] = body["parents"]
        file = self.add(body["name"], body["mimeType"], parent, content)
        self.created.append(file)
        return Request(file.as_answer)

    def update(self, fileId: str, body: dict[str, Any], **_: Any) -> Request:
        file = self.files_by_id[fileId]
        assert body == {"trashed": True}, f"update not understood: {body}"
        file.trashed = True
        return Request(file.as_answer)

    def in_folder(self, parent: str) -> DriveFiles:
        """The files in the folder, not in the bin."""
        return [f for f in self.files_by_id.values() if f.parent == parent and not f.trashed]

    @property
    def trashed(self) -> DriveFiles:
        return [f for f in self.files_by_id.values() if f.trashed]

    def named(self, name: str) -> DriveFiles:
        return [f for f in self.files_by_id.values() if f.name == name]


@dataclass
class Tab:
    sheet_id: int
    title: str
    rows: list[list[Any]] = field(default_factory=list[list[Any]])
    frozen_rows: int = 0
    bold_rows: set[int] = field(default_factory=set[int])
    colour: dict[str, Any] | None = None
    # The link each cell's text carries, by (row, column).
    links: dict[tuple[int, int], str] = field(default_factory=dict[tuple[int, int], str])
    # Each banding's range, by its id.
    bandings: dict[int, dict[str, Any]] = field(default_factory=dict[int, dict[str, Any]])
    basic_filter: dict[str, Any] | None = None


def _span(grid: dict[str, Any], start: str, end: str) -> tuple[float, float]:
    return grid.get(start, 0), grid.get(end, float("inf"))


def _overlap(a: dict[str, Any], b: dict[str, Any]) -> bool:
    def meet(start: str, end: str) -> bool:
        (a0, a1), (b0, b1) = _span(a, start, end), _span(b, start, end)
        return a0 < b1 and b0 < a1

    return meet("startRowIndex", "endRowIndex") and meet("startColumnIndex", "endColumnIndex")


# Requests the fake records and checks the shape of, but does not act on.
_RECORDED_ONLY = {"autoResizeDimensions", "updateDimensionProperties", "updateBorders"}


_RANGE = re.compile(r"^'((?:[^']|'')*)'")


def _tab_title(range_: str) -> str:
    found = _RANGE.match(range_)
    assert found, f"range should name its tab in quotes: {range_}"
    return found.group(1).replace("''", "'")


class _Values:
    def __init__(self, sheets: "FakeSheets") -> None:
        self._sheets = sheets

    def batchClear(self, spreadsheetId: str, body: dict[str, Any]) -> Request:
        def answer() -> dict[str, Any]:
            for range_ in body["ranges"]:
                self._sheets.tab(spreadsheetId, _tab_title(range_)).rows = []
            return {}

        return Request(answer)

    def batchUpdate(self, spreadsheetId: str, body: dict[str, Any]) -> Request:
        def answer() -> dict[str, Any]:
            self._sheets.value_input_options.append(body["valueInputOption"])
            for data in body["data"]:
                assert data["range"].endswith("!A1")
                tab = self._sheets.tab(spreadsheetId, _tab_title(data["range"]))
                values: list[list[Any]] = data["values"]
                # Values written from A1 replace only the cells they cover.
                tab.rows = [list(row) for row in values] + tab.rows[len(values) :]
            return {}

        return Request(answer)


class FakeSheets:
    """Sheets v4 for the spreadsheets that the fake Drive holds."""

    def __init__(self, drive: FakeDrive) -> None:
        self._drive = drive
        self._tabs: dict[str, list[Tab]] = {}
        self.value_input_options: list[str] = []
        # Every batchUpdate request, in the order sent.
        self.requests: list[dict[str, Any]] = []
        self._banding_ids = 0

    def spreadsheets(self) -> "FakeSheets":
        return self

    def values(self) -> _Values:
        return _Values(self)

    def tabs(self, spreadsheet_id: str) -> list[Tab]:
        assert self._drive.files_by_id[spreadsheet_id].mime_type == SPREADSHEET
        return self._tabs.setdefault(spreadsheet_id, [Tab(0, "Sheet1")])

    def tab(self, spreadsheet_id: str, title: str) -> Tab:
        [tab] = [t for t in self.tabs(spreadsheet_id) if t.title == title]
        return tab

    def by_id(self, spreadsheet_id: str, sheet_id: int) -> Tab:
        [tab] = [t for t in self.tabs(spreadsheet_id) if t.sheet_id == sheet_id]
        return tab

    def get(self, spreadsheetId: str, **_: Any) -> Request:
        return Request(
            lambda: {
                "sheets": [
                    {
                        "properties": {"sheetId": t.sheet_id, "title": t.title},
                        "bandedRanges": [
                            {"bandedRangeId": banding, "range": grid}
                            for banding, grid in t.bandings.items()
                        ],
                    }
                    for t in self.tabs(spreadsheetId)
                ]
            }
        )

    def batchUpdate(self, spreadsheetId: str, body: dict[str, Any]) -> Request:
        def answer() -> dict[str, Any]:
            tabs = self.tabs(spreadsheetId)
            replies: list[dict[str, Any]] = []
            for request in body["requests"]:
                self.requests.append(request)
                replies.append(self._apply(spreadsheetId, tabs, request))
            return {"replies": replies}

        return Request(answer)

    def sent(self, kind: str) -> list[dict[str, Any]]:
        """The body of every batchUpdate request of the kind, in the order sent."""
        return [r[kind] for r in self.requests if kind in r]

    def _apply(self, spreadsheet_id: str, tabs: list[Tab], request: dict[str, Any]) -> Any:
        [kind] = request
        body = request[kind]
        if kind == "addSheet":
            title = body["properties"]["title"]
            tab = Tab(max(t.sheet_id for t in tabs) + 1, title)
            tabs.append(tab)
            return {"addSheet": {"properties": {"sheetId": tab.sheet_id, "title": title}}}
        if kind == "deleteSheet":
            tab = self.by_id(spreadsheet_id, body["sheetId"])
            tabs.remove(tab)
            assert tabs, "a spreadsheet must keep at least one tab"
            return {}
        if kind == "updateSheetProperties":
            properties = body["properties"]
            tab = self.by_id(spreadsheet_id, properties["sheetId"])
            for name in body["fields"].split(","):
                if name == "index":
                    tabs.remove(tab)
                    tabs.insert(properties["index"], tab)
                elif name == "tabColorStyle":
                    tab.colour = properties["tabColorStyle"]
                elif name == "gridProperties.frozenRowCount":
                    tab.frozen_rows = properties["gridProperties"]["frozenRowCount"]
                else:
                    raise AssertionError(f"sheet property not understood: {name}")
            return {}
        if kind == "repeatCell":
            grid = body["range"]
            tab = self.by_id(spreadsheet_id, grid["sheetId"])
            fields = body["fields"].split(",")
            if fields == ["userEnteredFormat"]:
                tab.bold_rows.clear()
            if "userEnteredFormat.textFormat.bold" in fields:
                rows = range(grid["startRowIndex"], grid["endRowIndex"])
                bold = body["cell"]["userEnteredFormat"]["textFormat"]["bold"]
                tab.bold_rows = tab.bold_rows | set(rows) if bold else tab.bold_rows - set(rows)
            return {}
        if kind == "updateCells":
            grid = body["range"]
            tab = self.by_id(spreadsheet_id, grid["sheetId"])
            for row, cells in enumerate(body["rows"], start=grid["startRowIndex"]):
                for column, cell in enumerate(cells["values"], start=grid["startColumnIndex"]):
                    value = cell["userEnteredValue"]
                    assert list(value) == ["stringValue"], f"not a text value: {value}"
                    tab.rows[row][column] = value["stringValue"]
                    tab.links.pop((row, column), None)
                    for run in cell.get("textFormatRuns", []):
                        tab.links[(row, column)] = run["format"]["link"]["uri"]
            return {}
        if kind == "setBasicFilter":
            grid = body["filter"]["range"]
            self.by_id(spreadsheet_id, grid["sheetId"]).basic_filter = grid
            return {}
        if kind == "addBanding":
            grid = body["bandedRange"]["range"]
            tab = self.by_id(spreadsheet_id, grid["sheetId"])
            # Sheets refuses a banding over rows another banding already covers.
            assert not any(_overlap(grid, other) for other in tab.bandings.values()), (
                "banded range overlaps"
            )
            self._banding_ids += 1
            tab.bandings[self._banding_ids] = grid
            return {"addBanding": {"bandedRange": {"bandedRangeId": self._banding_ids}}}
        if kind == "deleteBanding":
            [tab] = [t for t in tabs if body["bandedRangeId"] in t.bandings]
            del tab.bandings[body["bandedRangeId"]]
            return {}
        if kind in _RECORDED_ONLY:
            return {}
        raise AssertionError(f"request not understood: {request}")
