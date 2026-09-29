"""Tests at the run seam: failures are contained to one email, and a run can be repeated.

No orchestrator is involved. See ADR 0005 and ADR 0013.
"""

from collections import Counter
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from functools import partial
from pathlib import Path

import pytest
from busy_month import AUGUST, EMAILS, BusyMonth
from support import Collection, invoice_email, real_pdf

from invoice_collector.classifier import FakeClassifier
from invoice_collector.domain import Classification, Doubt, Email, EmailState
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


# A PDF that cannot be opened


def attached(pdf: bytes, message_id: str = "m-locked") -> Email:
    return replace(
        invoice_email("Slack", pdf, received=datetime(2026, 8, 3, 9, 0, tzinfo=UTC)),
        message_id=message_id,
    )


def state_of(collection: Collection, message_id: str) -> tuple[EmailState, str | None]:
    [email] = [e for e in collection.ledger.examined_emails(AUGUST) if e.message_id == message_id]
    return email.state, email.reason


def test_password_protected_pdf_is_saved_as_it_is_and_held_for_review(
    collection: Collection,
) -> None:
    locked = real_pdf("slack august", password="s3cret")

    result = collection.run([attached(locked)])

    assert result.summary == []
    [held] = result.pending
    assert (collection.tmp_path / held.file_link).read_bytes() == locked
    assert held.doubts[0] == Doubt(
        None, "the PDF is password-protected, so nothing could be read from it"
    )
    assert {d.field for d in held.doubts[1:]} == {"vendor", "invoice_date", "total", "currency"}
    assert state_of(collection, "m-locked") == (
        EmailState.NEEDS_REVIEW,
        "the PDF is password-protected, so nothing could be read from it, and 4 more",
    )


def test_damaged_pdf_is_saved_as_it_is_and_held_for_review(collection: Collection) -> None:
    damaged = b"%PDF-1.7\n1 0 obj << /Type /Catalog"

    result = collection.run([attached(damaged, "m-damaged")])

    [held] = result.pending
    assert (collection.tmp_path / held.file_link).read_bytes() == damaged
    assert held.doubts[0].reason == "the PDF is damaged, so nothing could be read from it"
    assert state_of(collection, "m-damaged")[0] is EmailState.NEEDS_REVIEW


def test_held_pdf_that_cannot_be_opened_gives_the_email_as_a_starting_point(
    collection: Collection,
) -> None:
    result = collection.run([attached(real_pdf("slack august", password="s3cret"))])

    [held] = result.pending
    assert (held.extraction.vendor, held.extraction.invoice_date) == ("Slack", date(2026, 8, 3))
    # No amount is guessed: the person enters it from the document.
    assert (held.extraction.total, held.extraction.currency) == (Decimal("0.00"), "XXX")
    assert held.file_link.endswith("pending/2026-08_Slack_0.00-XXX.pdf")


def test_pdf_that_opens_but_that_nothing_could_read_still_fails_so_a_later_run_tries_again(
    collection: Collection,
) -> None:
    result = collection.run([attached(real_pdf("a scan"), "m-scan")])

    assert result.pending == []
    state, reason = state_of(collection, "m-scan")
    assert state is EmailState.FAILED
    assert reason == "no prepared answer for this document"


def test_held_pdf_that_cannot_be_opened_is_not_read_again_by_a_second_run(
    collection: Collection,
) -> None:
    emails = [attached(real_pdf("slack august", password="s3cret"))]
    collection.run(emails)

    result = collection.run(emails)

    assert len(result.pending) == 1
    assert collection.saved_files() == ["pending"]
