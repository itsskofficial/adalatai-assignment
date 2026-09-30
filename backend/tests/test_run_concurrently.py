"""Tests at the run seam: emails examined concurrently give the same run as one at a time.

The examinations are handed to plain threads: a pool of the test's own, or the run's
several_at_once, which the collect command uses when asked for more than one at a time.
"""

import argparse
import threading
from collections import Counter
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from functools import partial
from pathlib import Path
from types import TracebackType
from typing import Self

import pytest
from busy_month import AUGUST, EMAILS, EXTRACTIONS, OPS, SLACK_PDF, BusyMonth
from test_cli import OFFLINE, write_samples

from invoice_collector.browser import ThreadConfinedBrowser
from invoice_collector.classifier import Classifier, FakeClassifier
from invoice_collector.cli import add_collection_options, run_collection
from invoice_collector.destinations import DestinationPolicy
from invoice_collector.domain import (
    Attachment,
    Classification,
    CollectionMonth,
    Email,
    EmailState,
    Extraction,
)
from invoice_collector.extractor import FakeExtractor
from invoice_collector.ledger import Ledger
from invoice_collector.portal import LoginGated
from invoice_collector.run import Examination, RunResult, Settings, collect, several_at_once


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


class ReadingTogether:
    """Reads each document only once the other examinations are reading theirs too."""

    def __init__(self, parties: int) -> None:
        self._together = threading.Barrier(parties, timeout=10)
        self._reader = FakeExtractor.for_documents(SECOND_SLACK)

    def extract(self, pdf: bytes) -> Extraction:
        self._together.wait()
        return self._reader.extract(pdf)


SLACK_AGAIN_PDF = b"%PDF-1.7 another slack invoice"
SECOND_SLACK = {
    SLACK_PDF: EXTRACTIONS[SLACK_PDF],
    SLACK_AGAIN_PDF: replace(EXTRACTIONS[SLACK_PDF], total=Decimal("98.00")),
}


def test_a_second_invoice_read_at_the_same_time_as_the_first_is_still_doubted(
    one_at_a_time: BusyMonth, concurrently: BusyMonth
) -> None:
    first = next(e for e in EMAILS if e.message_id == "m-slack")
    second = replace(
        first,
        message_id="m-slack-again",
        attachments=(Attachment("invoice.pdf", "application/pdf", SLACK_AGAIN_PDF),),
    )
    one_at_a_time_pipeline = replace(
        one_at_a_time.pipeline(), extractor=FakeExtractor.for_documents(SECOND_SLACK)
    )
    collect(
        AUGUST,
        sources=one_at_a_time.sources([first, second]),
        pipeline=one_at_a_time_pipeline,
        summary_writers=[],
    )

    collect(
        AUGUST,
        sources=concurrently.sources([first, second]),
        pipeline=replace(concurrently.pipeline(), extractor=ReadingTogether(2)),
        summary_writers=[],
        examine_all=on_a_thread_pool,
    )

    expected = sorted(one_at_a_time.states().values())
    assert expected == [
        (EmailState.COLLECTED.value, None),
        (EmailState.NEEDS_REVIEW.value, "second invoice from Slack this month"),
    ]
    assert sorted(concurrently.states().values()) == expected


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


def test_by_default_an_examination_that_raises_is_recorded_as_failed_and_the_run_goes_on(
    one_at_a_time: BusyMonth,
) -> None:
    collect(
        AUGUST,
        sources=one_at_a_time.sources(),
        pipeline=one_at_a_time.pipeline(Crashing("m-zoom")),
        summary_writers=[],
        settings=Settings(retry_delays=()),
    )

    assert one_at_a_time.states()["m-zoom"] == (
        EmailState.FAILED.value,
        "could not be examined: ConnectionResetError: the connection was reset",
    )
    assert one_at_a_time.states()["m-slack"][0] == EmailState.COLLECTED.value


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

    def given_up(examinations: Sequence[Examination]) -> None:
        # As the run does once an examination has spent its retries.
        for examination in examinations:
            examination.fail("could not be examined: the connection was reset")

    result = collect(
        AUGUST,
        sources=concurrently.sources(slack),
        pipeline=concurrently.pipeline(),
        summary_writers=[],
        examine_all=given_up,
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


# Several at once, on plain threads, as the collect command's --max-concurrent asks.


def at_once(month: BusyMonth, at_most: int = 4, classifier: Classifier | None = None) -> RunResult:
    return collect(
        AUGUST,
        sources=month.sources(),
        pipeline=month.pipeline(classifier),
        summary_writers=[],
        settings=Settings(at_once=at_most, retry_delays=(0.0, 0.0)),
    )


def test_several_at_once_gives_the_same_run_as_one_at_a_time(
    one_at_a_time: BusyMonth, concurrently: BusyMonth
) -> None:
    expected = run_one_at_a_time(one_at_a_time)

    result = at_once(concurrently)

    assert result.summary == expected.summary
    assert result.gaps == expected.gaps
    assert result.failed_source_accounts == expected.failed_source_accounts
    assert sorted(result.warnings) == sorted(expected.warnings)
    assert concurrently.states() == one_at_a_time.states()


class InFlight:
    """A classifier that records how many calls are under way at once.

    Each call waits until as many calls as the limit are under way together, or until
    the others have all gone by, so the limit is reached whenever the run allows it.
    """

    def __init__(self, limit: int) -> None:
        self._rest = FakeClassifier(failing=frozenset({"m-unclassified"}))
        self._lock = threading.Condition()
        self._limit = limit
        self._now = 0
        self._reached = False
        self.most = 0

    def classify(self, email: Email) -> Classification:
        with self._lock:
            self._now += 1
            self.most = max(self.most, self._now)
            if self._now >= self._limit:
                self._reached = True
                self._lock.notify_all()
            self._lock.wait_for(lambda: self._reached, timeout=10)
        try:
            return self._rest.classify(email)
        finally:
            with self._lock:
                self._now -= 1


def test_no_more_emails_are_examined_at_once_than_the_limit(concurrently: BusyMonth) -> None:
    classifier = InFlight(limit=3)

    at_once(concurrently, at_most=3, classifier=classifier)

    assert classifier.most == 3


class CountingCrashes(FakeClassifier):
    """Fails in a way nothing anticipated for one email, every time, and counts the tries."""

    def __init__(self, message_id: str, error: Exception | None = None) -> None:
        super().__init__(failing=frozenset({"m-unclassified"}))
        self._message_id = message_id
        self._error = error or ConnectionResetError("the connection was reset")
        self.attempts = 0

    def classify(self, email: Email) -> Classification:
        if email.message_id == self._message_id:
            self.attempts += 1
            raise self._error
        return super().classify(email)


def test_an_examination_that_raises_is_retried_then_recorded_as_failed_at_once(
    concurrently: BusyMonth,
) -> None:
    classifier = CountingCrashes("m-zoom")

    result = at_once(concurrently, classifier=classifier)

    assert classifier.attempts == 3
    state, reason = concurrently.states()["m-zoom"]
    assert state == EmailState.FAILED.value
    assert reason is not None and "the connection was reset" in reason
    assert "Slack" in [row.vendor for row in result.summary]


def test_a_fault_in_the_code_is_not_retried_at_once(concurrently: BusyMonth) -> None:
    classifier = CountingCrashes("m-zoom", TypeError("unsupported operand"))

    at_once(concurrently, classifier=classifier)

    assert classifier.attempts == 1
    assert concurrently.states()["m-zoom"] == (
        EmailState.FAILED.value,
        "could not be examined: TypeError: unsupported operand",
    )


def test_retries_at_once_wait_as_the_settings_say(concurrently: BusyMonth) -> None:
    waited: list[float] = []

    collect(
        AUGUST,
        sources=concurrently.sources(),
        pipeline=concurrently.pipeline(Crashing("m-zoom")),
        summary_writers=[],
        examine_all=partial(
            several_at_once, at_most=4, retry_delays=(10.0, 20.0), sleep=waited.append
        ),
    )

    assert waited == [10.0, 20.0]


class CountingExtractor:
    def __init__(self) -> None:
        self._answers = FakeExtractor.for_documents(EXTRACTIONS)
        self._lock = threading.Lock()
        self.read = 0

    def extract(self, pdf: bytes) -> Extraction:
        with self._lock:
            self.read += 1
        return self._answers.extract(pdf)


def test_a_document_in_two_source_accounts_is_read_and_saved_once_at_once(
    concurrently: BusyMonth,
) -> None:
    slack = next(e for e in EMAILS if e.message_id == "m-slack")
    copy = replace(slack, source_account=OPS, message_id="m-slack-copy")
    extractor = CountingExtractor()

    result = collect(
        AUGUST,
        sources=concurrently.sources([slack, copy]),
        pipeline=replace(concurrently.pipeline(), extractor=extractor),
        summary_writers=[],
        settings=Settings(at_once=4),
    )

    assert extractor.read == 1
    [row] = result.summary
    assert row.source_accounts == tuple(sorted({slack.source_account, OPS}))
    saved = list((concurrently.folder / "archive" / "2026-08").iterdir())
    assert [p.read_bytes() for p in saved] == [SLACK_PDF]


def test_what_escapes_an_examination_stops_the_run_at_once(concurrently: BusyMonth) -> None:
    class MachineWentDown(BaseException):
        pass

    class GoingDown(FakeClassifier):
        def classify(self, email: Email) -> Classification:
            raise MachineWentDown

    with pytest.raises(MachineWentDown):
        at_once(concurrently, classifier=GoingDown())

    [crashed] = concurrently.ledger.runs()
    assert crashed.finished_at is None


def test_emails_not_yet_begun_are_dropped_when_an_examination_escapes(
    concurrently: BusyMonth,
) -> None:
    class MachineWentDown(BaseException):
        pass

    class GoingDown(FakeClassifier):
        def __init__(self) -> None:
            super().__init__()
            self.seen: list[str] = []

        def classify(self, email: Email) -> Classification:
            self.seen.append(email.message_id)
            raise MachineWentDown

    classifier = GoingDown()

    # One at a time, so the rest are still queued when the first escapes: none of them
    # is examined, not even retried, before the run stops.
    with pytest.raises(MachineWentDown):
        at_once(concurrently, at_most=1, classifier=classifier)

    assert len(classifier.seen) == 1


def test_at_least_one_email_is_examined_at_a_time() -> None:
    with pytest.raises(ValueError, match="at least one"):
        several_at_once([], at_most=0)


class RecordingBrowser:
    """Stands in for the headless browser, and records the thread of every call."""

    def __init__(self, threads: list[int]) -> None:
        self._threads = threads

    def __enter__(self) -> Self:
        self._threads.append(threading.get_ident())
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._threads.append(threading.get_ident())

    def render_html(self, html: str) -> bytes:
        self._threads.append(threading.get_ident())
        return html.encode()

    def fetch(self, url: str) -> bytes | LoginGated:
        self._threads.append(threading.get_ident())
        return LoginGated()


def test_the_browser_is_only_ever_used_from_the_thread_that_started_it() -> None:
    threads: list[int] = []

    def factory(policy: DestinationPolicy) -> AbstractContextManager[RecordingBrowser]:
        return RecordingBrowser(threads)

    with ThreadConfinedBrowser(DestinationPolicy(), factory) as browser:
        callers = [
            threading.Thread(target=lambda: (browser.render_html("<p/>"), browser.fetch("x")))
            for _ in range(4)
        ]
        for caller in callers:
            caller.start()
        for caller in callers:
            caller.join()

    assert len(threads) == 10
    assert len(set(threads)) == 1
    assert threads[0] != threading.get_ident()


def test_the_browsers_thread_is_let_go_when_the_browser_cannot_open() -> None:
    class CannotOpen(RecordingBrowser):
        def __enter__(self) -> Self:
            raise RuntimeError("no Chromium")

    def factory(policy: DestinationPolicy) -> AbstractContextManager[RecordingBrowser]:
        return CannotOpen([])

    before = threading.active_count()
    with pytest.raises(RuntimeError, match="no Chromium"):
        ThreadConfinedBrowser(DestinationPolicy(), factory).__enter__()

    assert threading.active_count() == before


def test_the_collect_command_reads_several_emails_at_once_when_asked(tmp_path: Path) -> None:
    samples, out = tmp_path / "samples", tmp_path / "out"
    write_samples(samples)
    threads: list[int] = []

    def browser(policy: DestinationPolicy) -> AbstractContextManager[RecordingBrowser]:
        return RecordingBrowser(threads)

    exit_code = run_collection(
        CollectionMonth(2026, 8),
        _collect_options(
            ["2026-08", "--samples", str(samples), "--out", str(out), "--max-concurrent", "3"]
        ),
        browser=browser,
    )

    assert exit_code == 0
    assert (out / "archive/2026-08/2026-08_Figma_190.00-USD.pdf").exists()
    # The browser was opened and closed on a thread of its own.
    assert len(threads) == 2
    assert len(set(threads)) == 1 and threads[0] != threading.get_ident()


def test_the_collect_command_examines_one_email_at_a_time_by_default() -> None:
    assert _collect_options(["2026-08", "--samples", "s"]).max_concurrent == 1


@pytest.mark.parametrize("given", ["0", "-2", "many"])
def test_the_collect_command_refuses_fewer_than_one_email_at_a_time(given: str) -> None:
    with pytest.raises(SystemExit):
        _collect_options(["2026-08", "--samples", "s", "--max-concurrent", given])


def _collect_options(arguments: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("month")
    add_collection_options(parser)
    return parser.parse_args([*arguments, *OFFLINE])
