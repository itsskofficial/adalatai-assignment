"""Tests at the run seam: each run is recorded in the ledger, as the Runs screen shows it."""

import sqlite3
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from busy_month import AUGUST, ENGINEERING, EXTRACTIONS, FINANCE, OPS, BusyMonth

from invoice_collector.classifier import FakeClassifier
from invoice_collector.domain import Classification, Email, Extraction, ModelUsage, Run, Sync
from invoice_collector.extractor import NO_HINTS, FakeExtractor, Hints
from invoice_collector.ledger import Ledger
from invoice_collector.mail_source import InMemoryMailSource
from invoice_collector.metering import Meter, RunMeter
from invoice_collector.run import (
    Examination,
    ExamineAll,
    Pipeline,
    RunResult,
    Settings,
    collect,
)

START = datetime(2026, 9, 3, 0, 30, tzinfo=UTC)


class Clock:
    """Moves on a minute each time it is read."""

    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        at = self.now
        self.now += timedelta(minutes=1)
        return at


@pytest.fixture
def month(tmp_path: Path) -> Iterator[BusyMonth]:
    ledger = Ledger(tmp_path / "ledger.sqlite")
    yield BusyMonth(tmp_path, ledger)
    ledger.close()


def run(month: BusyMonth, **options: object) -> RunResult:
    return collect(
        AUGUST,
        sources=options.pop("sources", month.sources()),  # pyright: ignore[reportArgumentType]
        pipeline=options.pop("pipeline", month.pipeline()),  # pyright: ignore[reportArgumentType]
        summary_writers=[],
        settings=Settings(retry_delays=()),
        now=options.pop("now", Clock()),  # pyright: ignore[reportArgumentType]
        **options,  # pyright: ignore[reportArgumentType]
    )


def test_a_run_is_recorded_with_how_it_was_started_and_how_long_it_took(
    month: BusyMonth,
) -> None:
    result = run(month, started_by="schedule")

    [recorded] = month.ledger.runs()
    assert recorded.id == result.run_id
    assert (recorded.collection_month, recorded.started_by) == (AUGUST, "schedule")
    assert (recorded.started_at, recorded.finished_at) == (START, START + timedelta(minutes=1))
    assert recorded.duration == timedelta(minutes=1)


def test_a_run_started_from_the_command_line_says_so_by_default(month: BusyMonth) -> None:
    run(month)

    assert [r.started_by for r in month.ledger.runs()] == ["command_line"]


def test_a_run_is_recorded_with_the_outcome_of_each_email_it_examined(
    month: BusyMonth,
) -> None:
    run(month)

    [recorded] = month.ledger.runs()
    assert (recorded.collected, recorded.needs_review, recorded.skipped, recorded.failed) == (
        5,
        0,
        1,
        3,
    )


def test_a_run_counts_only_the_emails_it_examined(month: BusyMonth) -> None:
    run(month)
    ops_only = [s for s in month.sources() if s.source_account == OPS]

    run(month, sources=ops_only)

    latest = month.ledger.runs()[0]
    assert (latest.collected, latest.needs_review, latest.skipped, latest.failed) == (3, 0, 0, 2)


def test_a_run_names_each_source_account_that_failed_and_why(month: BusyMonth) -> None:
    run(month)

    [recorded] = month.ledger.runs()
    assert recorded.source_accounts == (
        Sync(ENGINEERING, True, None),
        Sync(FINANCE, False, "the sign-in has expired"),
        Sync(OPS, True, None),
    )
    assert recorded.failed_source_accounts == (Sync(FINANCE, False, "the sign-in has expired"),)


def test_an_earlier_run_keeps_its_failure_after_the_account_is_read_again(
    month: BusyMonth,
) -> None:
    run(month)
    recovered = InMemoryMailSource(FINANCE, [])

    run(month, sources=[recovered])

    latest, earlier = month.ledger.runs()
    assert latest.failed_source_accounts == ()
    assert [s.source_account for s in earlier.failed_source_accounts] == [FINANCE]
    # Gaps use the latest word on each source account.
    assert all(s.succeeded for s in month.ledger.syncs(AUGUST))


def test_model_cost_is_left_empty_when_the_run_is_not_metered(month: BusyMonth) -> None:
    result = run(month)

    [recorded] = month.ledger.runs()
    assert (recorded.model_cost_usd, recorded.models) == (None, ())
    assert result.model_usage is None


# What the run's model calls cost


class ClassifiedByAModel:
    """Classifies as the fake does, and reports each call to the meter as a model would."""

    def __init__(self, meter: Meter, model: str = "jev-latest") -> None:
        self._meter = meter
        self._model = model
        self._fake = FakeClassifier(failing=frozenset({"m-unclassified"}))

    def classify(self, email: Email) -> Classification:
        answer = self._fake.classify(email)
        self._meter.record(self._model, 212, 8)
        return answer


class ReadByAModel:
    def __init__(self, meter: Meter) -> None:
        self._meter = meter
        self._fake = FakeExtractor.for_documents(EXTRACTIONS)

    def extract(self, pdf: bytes, hints: Hints = NO_HINTS) -> Extraction:
        extraction = self._fake.extract(pdf)
        self._meter.record("claude-haiku-4-5", 2365, 57)
        return extraction


def metered(month: BusyMonth, meter: RunMeter, classifier_model: str = "jev-latest") -> Pipeline:
    return replace(
        month.pipeline(),
        classifier=ClassifiedByAModel(meter, classifier_model),
        extractor=ReadByAModel(meter),
        meter=meter,
    )


def on_a_thread_pool(examinations: Sequence[Examination]) -> None:
    with ThreadPoolExecutor(max_workers=4) as pool:
        for done in [pool.submit(examination) for examination in examinations]:
            done.result()


def test_a_run_records_the_calls_tokens_and_cost_of_each_model(month: BusyMonth) -> None:
    result = run(month, pipeline=metered(month, RunMeter()))

    [recorded] = month.ledger.runs()
    # Of nine emails, eight were classified and five billing documents were read.
    assert recorded.models == (
        ModelUsage("claude-haiku-4-5", 5, 11825, 285, Decimal("0.013250")),
        ModelUsage("jev-latest", 8, 1696, 64, Decimal("0.000071232")),
    )
    assert recorded.model_cost_usd == Decimal("0.013321232")
    assert result.model_usage == recorded.models


def test_calls_made_on_several_threads_at_once_are_all_counted(month: BusyMonth) -> None:
    meter = RunMeter()

    run(month, pipeline=metered(month, meter), examine_all=on_a_thread_pool)

    [recorded] = month.ledger.runs()
    assert [(m.model, m.calls) for m in recorded.models] == [
        ("claude-haiku-4-5", 5),
        ("jev-latest", 8),
    ]


def test_a_run_that_calls_no_model_costs_nothing(month: BusyMonth) -> None:
    run(month, pipeline=metered(month, RunMeter()), sources=[])

    [recorded] = month.ledger.runs()
    assert (recorded.model_cost_usd, recorded.models) == (Decimal(0), ())


def test_a_model_with_no_known_price_leaves_the_cost_unknown(month: BusyMonth) -> None:
    run(month, pipeline=metered(month, RunMeter(), classifier_model="jev-2-preview"))

    [recorded] = month.ledger.runs()
    assert recorded.model_cost_usd is None
    assert [(m.model, m.calls, m.cost_usd) for m in recorded.models] == [
        ("claude-haiku-4-5", 5, Decimal("0.013250")),
        ("jev-2-preview", 8, None),
    ]


def test_each_run_records_what_its_own_calls_cost(month: BusyMonth) -> None:
    run(month, pipeline=metered(month, RunMeter()))

    run(month, pipeline=metered(month, RunMeter()))

    latest, first = month.ledger.runs()
    # Collected emails are neither classified nor read again. Those that were skipped or
    # failed are examined afresh, and none of them has a document that can be read.
    assert [(m.model, m.calls) for m in latest.models] == [("jev-latest", 3)]
    assert [(m.model, m.calls) for m in first.models] == [
        ("claude-haiku-4-5", 5),
        ("jev-latest", 8),
    ]


class MachineWentDown(BaseException):
    pass


def crashing(examinations: Sequence[Examination]) -> None:
    raise MachineWentDown


def test_a_run_that_crashed_is_recorded_as_not_finished(month: BusyMonth) -> None:
    examine_all: ExamineAll = crashing
    with pytest.raises(MachineWentDown):
        run(month, examine_all=examine_all)

    run(month)

    latest, crashed = month.ledger.runs()
    assert crashed.finished_at is None and crashed.duration is None
    assert latest.finished_at is not None


def test_runs_are_listed_newest_first_and_by_month(month: BusyMonth) -> None:
    run(month)
    run(month, started_by="dashboard")

    assert [r.started_by for r in month.ledger.runs(AUGUST)] == ["dashboard", "command_line"]
    assert month.ledger.runs(AUGUST.__class__(2026, 7)) == []


# A ledger from before runs were recorded


BEFORE_RUNS = """
CREATE TABLE syncs (
    collection_month TEXT NOT NULL,
    source_account   TEXT NOT NULL,
    succeeded        INTEGER NOT NULL,
    reason           TEXT,
    PRIMARY KEY (collection_month, source_account)
);
INSERT INTO syncs VALUES ('2026-08', 'ops@nyayalabs.example', 0, 'sign-in expired');
"""


def test_syncs_recorded_before_runs_were_are_kept(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite"
    db = sqlite3.connect(path)
    db.executescript(BEFORE_RUNS)
    db.close()

    ledger = Ledger(path)
    kept = ledger.syncs(AUGUST)
    ledger.record_sync(AUGUST, OPS)
    now = ledger.syncs(AUGUST)
    runs: list[Run] = ledger.runs()
    ledger.close()

    assert kept == [Sync(OPS, False, "sign-in expired")]
    assert now == [Sync(OPS, True, None)]
    assert runs == []
