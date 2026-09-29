"""The ledger's file is written by the app and the runner at once, through one way of opening it."""

import sqlite3
import threading
from collections import Counter
from contextlib import closing, suppress
from datetime import UTC, datetime
from pathlib import Path

from invoice_collector.api.review_history import ReviewHistory
from invoice_collector.database import BUSY_TIMEOUT_SECONDS, connect
from invoice_collector.domain import CollectionMonth, EmailState
from invoice_collector.ledger import Ledger
from invoice_collector.run_requests import RunRequests
from invoice_collector.source_account_registry import SourceAccountRegistry

AUGUST = CollectionMonth(2026, 8)
AT = datetime(2026, 9, 3, 0, 30, tzinfo=UTC)


def test_the_file_keeps_its_journal_ahead_and_waits_for_a_lock(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite"
    Ledger(path).close()

    with closing(connect(path, read_only=True)) as db:
        [journal] = db.execute("PRAGMA journal_mode").fetchone()
        [waits] = db.execute("PRAGMA busy_timeout").fetchone()

    assert journal == "wal"
    assert waits == BUSY_TIMEOUT_SECONDS * 1000


def test_reading_never_creates_the_file(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite"

    with suppress(sqlite3.OperationalError):
        connect(path, read_only=True).close()

    assert not path.exists()


def test_two_writers_at_once_both_write_everything(tmp_path: Path) -> None:
    path = tmp_path / "out" / "ledger.sqlite"
    Ledger(path).close()
    together = threading.Barrier(2, timeout=10)
    problems: list[BaseException] = []

    def the_runner() -> None:
        # A run recording itself, as the runner does.
        ledger = Ledger(path)
        try:
            together.wait()
            for _ in range(50):
                run_id = ledger.start_run(AUGUST, "schedule", AT)
                ledger.record_sync(AUGUST, "ops@nyayalabs.example", run_id=run_id)
                ledger.finish_run(run_id, AT, Counter({EmailState.COLLECTED: 1}))
        except BaseException as problem:
            problems.append(problem)
        finally:
            ledger.close()

    def the_app() -> None:
        # The dashboard's modules, each opening the file itself.
        try:
            together.wait()
            for number in range(50):
                RunRequests(path).asked(AUGUST, None, "finance@nyayalabs.example", AT, 0)
                SourceAccountRegistry(path).signed_in(
                    f"account{number}@nyayalabs.example",
                    "connected",
                    "finance@nyayalabs.example",
                    AT,
                    signed_in_at=None,
                    fingerprint=None,
                )
                ReviewHistory(path)  # opening alone must not disturb the other writer
        except BaseException as problem:
            problems.append(problem)

    writers = [threading.Thread(target=the_runner), threading.Thread(target=the_app)]
    for writer in writers:
        writer.start()
    for writer in writers:
        writer.join()

    assert problems == []
    ledger = Ledger(path)
    try:
        assert len(ledger.runs(AUGUST)) == 50
        assert all(run.finished_at is not None for run in ledger.runs(AUGUST))
    finally:
        ledger.close()
    assert len(RunRequests(path).of(AUGUST)) == 50
    assert len(SourceAccountRegistry(path).addresses()) == 50


def test_a_writer_does_not_wait_for_a_reader_to_finish(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite"
    Ledger(path).close()

    with closing(connect(path, read_only=True)) as reader:
        # A read that stays open, as the Runs screen's while a run records itself.
        reader.execute("BEGIN")
        before = reader.execute("SELECT COUNT(*) FROM runs").fetchone()
        # Waits for nothing: with the journal kept ahead, the read holds no lock that a
        # write must wait for.
        with closing(sqlite3.connect(path, timeout=0)) as impatient, impatient:
            impatient.execute(
                "INSERT INTO runs (collection_month, started_by, started_at) "
                "VALUES ('2026-08', 'schedule', ?)",
                (AT.isoformat(),),
            )
        # The reader still sees what was there when it began.
        assert reader.execute("SELECT COUNT(*) FROM runs").fetchone() == before
        reader.execute("COMMIT")
