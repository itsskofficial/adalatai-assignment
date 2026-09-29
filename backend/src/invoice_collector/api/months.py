"""Which collection months a ledger holds.

The Ledger answers questions about one collection month and has no way to list them, so this
reads its SQLite file directly. It opens the file read-only and never creates it.
"""

import sqlite3
from contextlib import closing
from pathlib import Path

from invoice_collector.database import connect


def collection_months(ledger_path: Path) -> list[str]:
    """Collection months with at least one examined email, newest first."""
    if not ledger_path.is_file():
        return []
    with closing(connect(ledger_path, read_only=True)) as db:
        try:
            rows = db.execute(
                "SELECT DISTINCT collection_month FROM emails ORDER BY collection_month DESC"
            ).fetchall()
        except sqlite3.OperationalError:
            # A file with no emails table has had no run.
            return []
    return [str(month) for (month,) in rows]
