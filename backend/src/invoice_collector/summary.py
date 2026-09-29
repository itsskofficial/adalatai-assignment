"""The read-only report of a run, one row per charge."""

import csv
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from invoice_collector.domain import SummaryRow

COLUMNS = ("vendor", "date", "amount", "currency", "source_account", "file_link")


_FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")


class SummaryWriter(Protocol):
    def write(self, rows: Sequence[SummaryRow]) -> None: ...


def as_text_cell(value: str) -> str:
    """Stops a spreadsheet from running text taken from an email as a formula."""
    return f"'{value}" if value.startswith(_FORMULA_STARTS) else value


class CsvSummary:
    def __init__(self, path: Path) -> None:
        self._path = path

    def write(self, rows: Sequence[SummaryRow]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(COLUMNS)
            for row in rows:
                writer.writerow(
                    (
                        as_text_cell(row.vendor),
                        row.invoice_date.isoformat(),
                        f"{row.total:.2f}",
                        as_text_cell(row.currency),
                        as_text_cell(row.source_account),
                        as_text_cell(row.file_link),
                    )
                )
