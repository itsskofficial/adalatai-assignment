"""The read-only report of a run, one row per charge."""

import csv
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from invoice_collector.domain import SummaryRow

COLUMNS = ("vendor", "date", "amount", "currency", "source_account", "file_link")


class SummaryWriter(Protocol):
    def write(self, rows: Sequence[SummaryRow]) -> None: ...


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
                        row.vendor,
                        row.invoice_date.isoformat(),
                        f"{row.total:.2f}",
                        row.currency,
                        row.source_account,
                        row.file_link,
                    )
                )
