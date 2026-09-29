"""Tests at the run seam: emails examined concurrently give the same run as one at a time.

No orchestrator is involved: the examinations are handed to a plain thread pool.
"""

import subprocess
import sys
import threading
from collections import Counter
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from busy_month import AUGUST, EMAILS, BusyMonth

from invoice_collector.classifier import FakeClassifier
from invoice_collector.domain import Classification, Email, EmailState
from invoice_collector.ledger import Ledger
from invoice_collector.run import Examination, RunResult, collect


def on_a_thread_pool(examinations: Sequence[Examination]) -> None:
    with ThreadPoolExecutor(max_workers=4) as pool:
        for done in [pool.submit(examination) for examination in examinations]:
            done.result()


@pytest.fixture
def one_at_a_time(tmp_path: Path) -> Iterator[BusyMonth]:
    ledger = Ledger(tmp_path / "one_at_a_time" / "ledger.sqlite")
    yield BusyMonth(tmp_path / "one_at_a_time", ledger)
    ledger.close()


@pytest.fixture
def concurrently(tmp_path: Path) -> Iterator[BusyMonth]:
    ledger = Ledger(tmp_path / "concurrently" / "ledger.sqlite")
    yield BusyMonth(tmp_path / "concurrently", ledger)
    ledger.close()


def run_one_at_a_time(month: BusyMonth) -> RunResult:
    return collect(AUGUST, sources=month.sources(), pipeline=month.pipeline(), summary_writers=[])


def test_examining_on_a_thread_pool_gives_the_same_run_as_one_at_a_time(
    one_at_a_time: BusyMonth, concurrently: BusyMonth
) -> None:
    expected = run_one_at_a_time(one_at_a_time)

    result = collect(
        AUGUST,
        sources=concurrently.sources(),
        pipeline=concurrently.pipeline(),
        summary_writers=[],
        examine_all=on_a_thread_pool,
    )

    assert result.summary == expected.summary
    assert result.gaps == expected.gaps
    assert result.failed_source_accounts == expected.failed_source_accounts
    assert sorted(result.warnings) == sorted(expected.warnings)
    assert concurrently.states() == one_at_a_time.states()


def test_the_busy_month_includes_failures_and_warnings(one_at_a_time: BusyMonth) -> None:
    result = run_one_at_a_time(one_at_a_time)

    states = Counter(state for state, _ in one_at_a_time.states().values())
    assert states == {"collected": 5, "failed": 3, "skipped": 1}
    assert result.warnings
    assert result.failed_source_accounts


def test_every_examination_is_performed_exactly_once(concurrently: BusyMonth) -> None:
    performed: list[str] = []
    lock = threading.Lock()

    def counting(examinations: Sequence[Examination]) -> None:
        def perform(examination: Examination) -> None:
            with lock:
                performed.append(examination.email.message_id)
            examination()

        with ThreadPoolExecutor(max_workers=4) as pool:
            for done in [pool.submit(perform, e) for e in examinations]:
                done.result()

    collect(
        AUGUST,
        sources=concurrently.sources(),
        pipeline=concurrently.pipeline(),
        summary_writers=[],
        examine_all=counting,
    )

    assert sorted(performed) == sorted(e.message_id for e in EMAILS)


class Crashing:
    """A classifier that fails in a way nothing anticipated, for one email."""

    def __init__(self, message_id: str) -> None:
        self._message_id = message_id
        self._rest = FakeClassifier()

    def classify(self, email: Email) -> Classification:
        if email.message_id == self._message_id:
            raise ConnectionResetError("the connection was reset")
        return self._rest.classify(email)


def test_by_default_an_examination_that_raises_stops_the_run_as_before(
    one_at_a_time: BusyMonth,
) -> None:
    with pytest.raises(ConnectionResetError):
        collect(
            AUGUST,
            sources=one_at_a_time.sources(),
            pipeline=one_at_a_time.pipeline(Crashing("m-zoom")),
            summary_writers=[],
        )


def recording_failures(examinations: Sequence[Examination]) -> None:
    for examination in examinations:
        try:
            examination()
        except ConnectionResetError as error:
            examination.fail(f"could not be examined: {error}")


def test_an_examination_that_raises_can_be_recorded_as_failed(concurrently: BusyMonth) -> None:
    collect(
        AUGUST,
        sources=concurrently.sources(),
        pipeline=concurrently.pipeline(Crashing("m-zoom")),
        summary_writers=[],
        examine_all=recording_failures,
    )

    assert concurrently.states()["m-zoom"] == (
        EmailState.FAILED.value,
        "could not be examined: the connection was reset",
    )
    assert concurrently.states()["m-slack"][0] == EmailState.COLLECTED.value


def test_a_failure_recorded_later_keeps_what_an_earlier_run_collected(
    concurrently: BusyMonth,
) -> None:
    slack = [e for e in EMAILS if e.message_id == "m-slack"]
    collect(
        AUGUST,
        sources=concurrently.sources(slack),
        pipeline=concurrently.pipeline(),
        summary_writers=[],
    )

    result = collect(
        AUGUST,
        sources=concurrently.sources(slack),
        pipeline=concurrently.pipeline(Crashing("m-slack")),
        summary_writers=[],
        examine_all=recording_failures,
    )

    assert concurrently.states()["m-slack"][0] == EmailState.COLLECTED.value
    assert [row.vendor for row in result.summary] == ["Slack"]
    assert any("kept what was collected before" in w for w in result.warnings)


def test_a_failure_is_not_recorded_for_an_email_that_belongs_to_another_month(
    concurrently: BusyMonth,
) -> None:
    # Received in July, so only the July run may record what became of it.
    july_email = replace(
        EMAILS[0], message_id="m-late", received_at=datetime(2026, 7, 29, 9, 0, tzinfo=UTC)
    )

    collect(
        AUGUST,
        sources=concurrently.sources([july_email]),
        pipeline=concurrently.pipeline(Crashing("m-late")),
        summary_writers=[],
        examine_all=recording_failures,
    )

    assert "m-late" not in concurrently.states()


def test_the_core_does_not_import_the_orchestrator() -> None:
    probe = (
        "import sys\n"
        "import invoice_collector.cli, invoice_collector.ledger, invoice_collector.run\n"
        "print(sorted(m for m in sys.modules if m.split('.')[0] == 'prefect'))\n"
    )

    shown = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )

    assert shown.stdout.strip() == "[]"
