"""The Prefect layer: a run as a flow, each email as a task.

Runs against Prefect's test harness: a temporary local server and database, started
once for this module. Nothing leaves this machine.
"""

import csv
import threading
import time
from collections.abc import Iterator
from contextlib import AbstractContextManager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Self
from zoneinfo import ZoneInfo

import pytest
from busy_month import AUGUST, EMAILS, EXTRACTIONS, OPS, SLACK_PDF, BusyMonth
from prefect.client.orchestration import get_client
from prefect.client.schemas.filters import (
    ArtifactFilter,
    ArtifactFilterFlowRunId,
    ArtifactFilterKey,
)
from prefect.testing.utilities import prefect_test_harness
from test_cli import OFFLINE, write_samples

from invoice_collector.classifier import FakeClassifier
from invoice_collector.destinations import DestinationPolicy
from invoice_collector.domain import (
    Classification,
    CollectionMonth,
    Email,
    EmailState,
    Extraction,
    Gap,
)
from invoice_collector.extractor import FakeExtractor
from invoice_collector.ledger import Ledger
from invoice_collector.orchestration import (
    MONTHLY,
    RunInputs,
    ThreadConfinedBrowser,
    collect_month,
    collect_with_options,
    main,
    month_before,
    run_summary,
    serve_monthly,
)
from invoice_collector.portal import LoginGated
from invoice_collector.run import Pipeline, RunResult, collect

pytestmark = pytest.mark.orchestration

KOLKATA = ZoneInfo("Asia/Kolkata")


@pytest.fixture(scope="module", autouse=True)
def prefect_server() -> Iterator[None]:
    with prefect_test_harness(server_startup_timeout=60):
        yield


@pytest.fixture
def one_at_a_time(tmp_path: Path) -> Iterator[BusyMonth]:
    ledger = Ledger(tmp_path / "one_at_a_time" / "ledger.sqlite")
    yield BusyMonth(tmp_path / "one_at_a_time", ledger)
    ledger.close()


@pytest.fixture
def as_a_flow(tmp_path: Path) -> Iterator[BusyMonth]:
    ledger = Ledger(tmp_path / "as_a_flow" / "ledger.sqlite")
    yield BusyMonth(tmp_path / "as_a_flow", ledger)
    ledger.close()


def test_the_flow_gives_the_same_run_as_collect(
    one_at_a_time: BusyMonth, as_a_flow: BusyMonth
) -> None:
    expected = collect(
        AUGUST,
        sources=one_at_a_time.sources(),
        pipeline=one_at_a_time.pipeline(),
        summary_writers=[],
    )

    result = collect_month(AUGUST, inputs(as_a_flow))

    assert result.summary == expected.summary
    assert result.gaps == expected.gaps
    assert result.failed_source_accounts == expected.failed_source_accounts
    assert sorted(result.warnings) == sorted(expected.warnings)
    assert as_a_flow.states() == one_at_a_time.states()


def inputs(
    month: BusyMonth, pipeline: Pipeline | None = None, emails: list[Email] | None = None
) -> RunInputs:
    return RunInputs(
        sources=month.sources(emails),
        pipeline=pipeline or month.pipeline(),
        summary_writers=[],
    )


class InFlight:
    """A classifier that takes a while, and records how many calls overlap."""

    def __init__(self) -> None:
        self._rest = FakeClassifier(failing=frozenset({"m-unclassified"}))
        self._lock = threading.Lock()
        self._now = 0
        self.most = 0

    def classify(self, email: Email) -> Classification:
        with self._lock:
            self._now += 1
            self.most = max(self.most, self._now)
        try:
            time.sleep(0.2)
            return self._rest.classify(email)
        finally:
            with self._lock:
                self._now -= 1


def test_no_more_emails_are_examined_at_once_than_the_cap(as_a_flow: BusyMonth) -> None:
    classifier = InFlight()

    collect_month(AUGUST, inputs(as_a_flow, as_a_flow.pipeline(classifier)), max_concurrent=3)

    assert classifier.most <= 3


class Crashing:
    """Fails in a way nothing anticipated for one email, every time it is asked."""

    def __init__(self, message_id: str, error: Exception | None = None) -> None:
        self._message_id = message_id
        self._error = error or ConnectionResetError("the connection was reset")
        self._rest = FakeClassifier(failing=frozenset({"m-unclassified"}))
        self.attempts = 0

    def classify(self, email: Email) -> Classification:
        if email.message_id == self._message_id:
            self.attempts += 1
            raise self._error
        return self._rest.classify(email)


def test_an_examination_that_raises_is_retried_then_recorded_as_failed(
    as_a_flow: BusyMonth,
) -> None:
    classifier = Crashing("m-zoom")

    result = collect_month(
        AUGUST,
        inputs(as_a_flow, as_a_flow.pipeline(classifier)),
        retries=2,
        retry_delay_seconds=0,
    )

    assert classifier.attempts == 3
    state, reason = as_a_flow.states()["m-zoom"]
    assert state == EmailState.FAILED.value
    assert reason is not None and "the connection was reset" in reason
    assert "Slack" in [row.vendor for row in result.summary]


def test_a_fault_in_the_code_is_recorded_as_failed_without_retrying(
    as_a_flow: BusyMonth,
) -> None:
    classifier = Crashing("m-zoom", TypeError("unsupported operand"))

    collect_month(
        AUGUST,
        inputs(as_a_flow, as_a_flow.pipeline(classifier)),
        retries=2,
        retry_delay_seconds=0,
    )

    assert classifier.attempts == 1
    assert as_a_flow.states()["m-zoom"] == (
        EmailState.FAILED.value,
        "could not be examined: TypeError: unsupported operand",
    )


def test_an_anticipated_failure_is_not_retried(as_a_flow: BusyMonth) -> None:
    asked: list[str] = []

    class Counting(FakeClassifier):
        def classify(self, email: Email) -> Classification:
            asked.append(email.message_id)
            return super().classify(email)

    counting = Counting(failing=frozenset({"m-unclassified"}))
    collect_month(AUGUST, inputs(as_a_flow, as_a_flow.pipeline(counting)), retry_delay_seconds=0)

    assert asked.count("m-unclassified") == 1
    assert as_a_flow.states()["m-unclassified"][0] == EmailState.FAILED.value


class CountingExtractor:
    def __init__(self) -> None:
        self._answers = FakeExtractor.for_documents(EXTRACTIONS)
        self._lock = threading.Lock()
        self.read = 0

    def extract(self, pdf: bytes) -> Extraction:
        with self._lock:
            self.read += 1
        return self._answers.extract(pdf)


def test_a_document_found_in_two_source_accounts_is_read_and_saved_once(
    as_a_flow: BusyMonth,
) -> None:
    slack = next(e for e in EMAILS if e.message_id == "m-slack")
    copy = replace(slack, source_account=OPS, message_id="m-slack-copy")
    extractor = CountingExtractor()

    result = collect_month(
        AUGUST,
        inputs(as_a_flow, replace(as_a_flow.pipeline(), extractor=extractor), [slack, copy]),
    )

    assert extractor.read == 1
    [row] = result.summary
    assert row.source_accounts == tuple(sorted({slack.source_account, OPS}))
    saved = list((as_a_flow.folder / "archive" / "2026-08").iterdir())
    assert [p.read_bytes() for p in saved] == [SLACK_PDF]


def test_the_month_collected_on_schedule_is_the_one_before() -> None:
    assert month_before(datetime(2026, 9, 3, 6, 0, tzinfo=KOLKATA)) == CollectionMonth(2026, 8)
    assert month_before(datetime(2027, 1, 3, 6, 0, tzinfo=KOLKATA)) == CollectionMonth(2026, 12)


def test_the_scheduled_time_is_read_in_india() -> None:
    # 06:00 on the 3rd in Kolkata is still the 3rd in UTC; 23:00 UTC on the last
    # day of a month is already the 1st in Kolkata.
    assert month_before(datetime(2026, 9, 3, 0, 30, tzinfo=UTC)) == CollectionMonth(2026, 8)
    assert month_before(datetime(2026, 8, 31, 23, 0, tzinfo=UTC)) == CollectionMonth(2026, 8)


def test_the_schedule_is_the_third_of_each_month_at_six_in_india() -> None:
    assert MONTHLY.cron == "0 6 3 * *"
    assert MONTHLY.timezone == "Asia/Kolkata"


def test_the_run_summary_gives_the_headline_numbers() -> None:
    result = RunResult(
        summary=[],
        warnings=["Notion 2026-08-14: no rate for EUR on 2026-08-14"],
        gaps=[Gap("Figma", "missing", "ops@nyayalabs.example", None)],
        failed_source_accounts={"finance@nyayalabs.example": "the sign-in has expired"},
    )

    text = run_summary(AUGUST, result, {"collected": 5, "failed": 3, "skipped": 1})

    assert "2026-08" in text
    assert "| Billing documents collected | 0 |" in text
    assert "| collected | 5 |" in text
    assert "| failed | 3 |" in text
    assert "Figma" in text and "missing" in text
    assert "finance@nyayalabs.example" in text and "the sign-in has expired" in text
    assert "no rate for EUR" in text


def test_the_flow_leaves_a_summary_of_the_run_in_prefect(as_a_flow: BusyMonth) -> None:
    state = collect_month(AUGUST, inputs(as_a_flow), return_state=True)
    flow_run_id = state.state_details.flow_run_id
    assert flow_run_id is not None

    with get_client(sync_client=True) as client:
        [artifact] = client.read_artifacts(
            artifact_filter=ArtifactFilter(
                key=ArtifactFilterKey(any_=["collection-2026-08"]),
                flow_run_id=ArtifactFilterFlowRunId(any_=[flow_run_id]),
            )
        )
    assert "| Billing documents collected | 5 |" in str(artifact.data)
    assert "| collected | 5 |" in str(artifact.data)


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


def test_the_run_command_collects_a_month_as_a_flow(tmp_path: Path) -> None:
    samples, out = tmp_path / "samples", tmp_path / "out"
    write_samples(samples)

    exit_code = main(["run", "2026-08", "--samples", str(samples), "--out", str(out), *OFFLINE])

    assert exit_code == 0
    assert (out / "archive/2026-08/2026-08_Figma_190.00-USD.pdf").exists()
    with (out / "2026-08_summary.csv").open(newline="", encoding="utf-8") as f:
        [row] = list(csv.DictReader(f))
    assert row["vendor"] == "Figma"


def test_the_serve_command_serves_the_collection_with_its_options(tmp_path: Path) -> None:
    served: list[list[str]] = []
    options = ["--samples", str(tmp_path), "--max-concurrent", "3", *OFFLINE]

    exit_code = main(["serve", *options], serve=served.append)

    assert exit_code == 0
    assert served == [options]


def runs_in(out: Path) -> list[str]:
    ledger = Ledger(out / "ledger.sqlite")
    started_by = [run.started_by for run in ledger.runs()]
    ledger.close()
    return started_by


def test_the_run_command_records_its_run_as_started_from_the_command_line(
    tmp_path: Path,
) -> None:
    samples, out = tmp_path / "samples", tmp_path / "out"
    write_samples(samples)

    main(["run", "2026-08", "--samples", str(samples), "--out", str(out), *OFFLINE])

    assert runs_in(out) == ["command_line"]


def test_a_run_on_the_schedule_is_recorded_as_started_by_it(tmp_path: Path) -> None:
    samples, out = tmp_path / "samples", tmp_path / "out"
    write_samples(samples)

    collect_with_options(
        ["2026-08", "--samples", str(samples), "--out", str(out), *OFFLINE], started_by="schedule"
    )

    assert runs_in(out) == ["schedule"]


def test_the_schedule_starts_each_run_as_a_run_on_the_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    served: list[object] = []

    def serve(**options: object) -> None:
        served.append(options["parameters"])

    monkeypatch.setattr(collect_with_options, "serve", serve)

    serve_monthly(["--samples", "samples"])

    assert served == [{"arguments": ["--samples", "samples"], "started_by": "schedule"}]
