"""Tests at the run seam: failures are contained to one email, and a run can be repeated.

No orchestrator is involved. See ADR 0005 and ADR 0013.
"""

from collections import Counter
from collections.abc import Iterator
from functools import partial
from pathlib import Path

import pytest
from busy_month import AUGUST, EMAILS, BusyMonth

from invoice_collector.classifier import FakeClassifier
from invoice_collector.domain import Classification, Email, EmailState
from invoice_collector.ledger import Ledger
from invoice_collector.run import RunResult, Settings, collect, one_after_another

NO_WAITING = Settings(retry_delays=(0.0, 0.0))


@pytest.fixture
def month(tmp_path: Path) -> Iterator[BusyMonth]:
    ledger = Ledger(tmp_path / "ledger.sqlite")
    yield BusyMonth(tmp_path, ledger)
    ledger.close()


class Flaky(FakeClassifier):
    """Fails for one email the first few times it is asked, as a dropped connection does."""

    def __init__(self, message_id: str, failures: int, error: Exception | None = None) -> None:
        super().__init__(failing=frozenset({"m-unclassified"}))
        self._message_id = message_id
        self._failures = failures
        self._error = error or ConnectionResetError("the connection was reset")
        self.asked: Counter[str] = Counter()

    def classify(self, email: Email) -> Classification:
        self.asked[email.message_id] += 1
        if email.message_id == self._message_id and self.asked[email.message_id] <= self._failures:
            raise self._error
        return super().classify(email)


def run(month: BusyMonth, classifier: Flaky, **options: object) -> RunResult:
    return collect(
        AUGUST,
        sources=month.sources(),
        pipeline=month.pipeline(classifier),
        summary_writers=[],
        settings=NO_WAITING,
        **options,  # pyright: ignore[reportArgumentType]
    )


# A temporary error


def test_temporary_error_is_retried_for_that_email_alone(month: BusyMonth) -> None:
    classifier = Flaky("m-zoom", failures=1)

    result = run(month, classifier)

    assert month.states()["m-zoom"] == (EmailState.COLLECTED.value, None)
    assert "Zoom" in [row.vendor for row in result.summary]
    assert classifier.asked["m-zoom"] == 2
    assert {classifier.asked[e.message_id] for e in EMAILS if e.message_id != "m-zoom"} == {1}


def test_error_that_persists_is_recorded_as_failed_once_the_retries_are_spent(
    month: BusyMonth,
) -> None:
    classifier = Flaky("m-zoom", failures=99)

    result = run(month, classifier)

    assert classifier.asked["m-zoom"] == 3
    assert month.states()["m-zoom"] == (
        EmailState.FAILED.value,
        "could not be examined: ConnectionResetError: the connection was reset",
    )
    assert "Slack" in [row.vendor for row in result.summary]


def test_retries_wait_longer_each_time(month: BusyMonth) -> None:
    waited: list[float] = []

    run(
        month,
        Flaky("m-zoom", failures=99),
        examine_all=partial(one_after_another, retry_delays=(10.0, 20.0), sleep=waited.append),
    )

    assert waited == [10.0, 20.0]


def test_by_default_a_run_waits_ten_then_twenty_seconds() -> None:
    assert Settings().retry_delays == (10.0, 20.0)


def test_fault_in_the_code_is_recorded_as_failed_without_retrying(month: BusyMonth) -> None:
    classifier = Flaky("m-zoom", failures=99, error=TypeError("unsupported operand"))

    run(month, classifier)

    assert classifier.asked["m-zoom"] == 1
    assert month.states()["m-zoom"] == (
        EmailState.FAILED.value,
        "could not be examined: TypeError: unsupported operand",
    )
