"""What the dashboard and the runner check before they start, and how they refuse.

Both name every missing or invalid setting at once, in plain words, and refuse to start,
so whoever deploys them fixes everything in one go rather than one restart at a time. An
owner account that is not signed in is not among them: it is connected on the dashboard's
own Source accounts screen (see owner_account.py).
"""

import os
import sys
from pathlib import Path


def ledger_problem(ledger_path: Path) -> str | None:
    """Why the ledger's folder cannot be written, or None when it can.

    A run writes the ledger, the archive and the summary there, and the dashboard writes
    decisions beside them, so a folder that is not writable is found before anything starts.
    """
    folder = ledger_path.parent
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        return f"The ledger's folder {folder} cannot be created: {error.strerror or error}."
    if not os.access(folder, os.W_OK | os.X_OK):
        return (
            f"The ledger's folder {folder} cannot be written by this user. Give it to the user "
            "the service runs as."
        )
    if ledger_path.exists() and not os.access(ledger_path, os.R_OK | os.W_OK):
        return f"The ledger {ledger_path} cannot be read and written by this user."
    return None


def refuse(who: str, problems: list[str]) -> None:
    """Says, on standard error, that the service cannot start and every reason why."""
    if len(problems) == 1:
        print(f"The {who} cannot start. {problems[0]}", file=sys.stderr)
        return
    print(f"The {who} cannot start, for {len(problems)} reasons:", file=sys.stderr)
    for problem in problems:
        print(f"- {problem}", file=sys.stderr)
