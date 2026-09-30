"""Opening the ledger's SQLite file, the one way every module opens it.

The app and the runner are two processes writing one file, and the dashboard's modules
open it beside the Ledger. So every connection waits for a lock another holds rather than
failing at once, and the file keeps its journal ahead of it (write-ahead logging), so a
reader never waits for a writer and a writer never waits for readers. Write-ahead logging
needs the processes on one machine sharing one disk, which is how the tool is deployed.
"""

import sqlite3
from pathlib import Path

# How long a connection waits for another to finish writing before it gives up. A run
# records each email in one short transaction, so a wait this long means something is stuck.
BUSY_TIMEOUT_SECONDS = 30.0


def connect(
    path: Path, *, read_only: bool = False, check_same_thread: bool = True
) -> sqlite3.Connection:
    """A connection to the ledger's file.

    Read-only never creates the file, and raises sqlite3.OperationalError when it is
    missing. Otherwise the file is created, with its folder, when it is not there yet, and
    set to write-ahead logging, which it then keeps for every connection.
    """
    if read_only:
        return sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=ro",
            uri=True,
            timeout=BUSY_TIMEOUT_SECONDS,
            check_same_thread=check_same_thread,
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=BUSY_TIMEOUT_SECONDS, check_same_thread=check_same_thread)
    try:
        db.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError as error:
        # Switching a file to write-ahead logging needs it to itself for a moment. When
        # another process holds it for longer than the wait, the connection is still good:
        # the file keeps whichever journal it has, and the next connection sets it.
        if "locked" not in str(error):
            db.close()
            raise
    return db
