"""The collect command traces the model calls of its run and sends them before it exits.

Claude is replayed by a local server; the tracer is a fake, or Langfuse at an address where
nothing answers. See ADR 0017.
"""

import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from conftest import ReplayClient
from test_cli import OFFLINE, write_samples

from invoice_collector import cli, tracing
from invoice_collector.ledger import Ledger
from invoice_collector.tracing import FakeTracer, Tracer

RECORDED = Path(__file__).parent / "recorded"
INVOICE: dict[str, Any] = json.loads((RECORDED / "claude_slack_invoice.json").read_text("utf-8"))
BY_CLAUDE = ["--extractor", "claude", "--classifier", "rules", "--no-exchange-rates"]


def collect(tmp_path: Path, options: list[str], replay_client: ReplayClient) -> int:
    samples, out = tmp_path / "samples", tmp_path / "out"
    write_samples(samples)
    return cli.main(
        ["collect", "2026-08", "--samples", str(samples), "--out", str(out), *options],
        claude_client=lambda: replay_client(200, INVOICE),
    )


def use(monkeypatch: pytest.MonkeyPatch, tracer: Tracer) -> None:
    monkeypatch.setattr(cli.tracing, "tracer_from_environment", lambda: tracer)


def test_the_collect_command_traces_its_run_and_sends_the_traces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replay_client: ReplayClient
) -> None:
    tracer = FakeTracer()
    use(monkeypatch, tracer)

    exit_code = collect(tmp_path, BY_CLAUDE, replay_client)

    assert exit_code == 0
    [extraction] = [c for c in tracer.calls if c.step == "extraction"]
    ledger = Ledger(tmp_path / "out" / "ledger.sqlite")
    [run] = ledger.runs()
    ledger.close()
    assert extraction.context.run_id == run.id
    assert extraction.context.collection_month == "2026-08"
    assert extraction.context.invoice_format == "attachment"
    assert extraction.finished is not None and extraction.finished.input_tokens == 2365
    assert tracer.flushes == 1


def test_a_tracer_that_fails_is_a_warning_and_the_run_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replay_client: ReplayClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    use(monkeypatch, FakeTracer(failing=True))

    exit_code = collect(tmp_path, BY_CLAUDE, replay_client)

    assert exit_code == 0
    assert "Warning: traces may not have been sent" in capsys.readouterr().err
    assert (tmp_path / "out" / "archive" / "2026-08").exists()


def test_without_langfuse_keys_nothing_is_traced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replay_client: ReplayClient
) -> None:
    chosen: list[Tracer] = []
    real = tracing.tracer_from_environment

    def choosing() -> Tracer:
        chosen.append(real())
        return chosen[-1]

    monkeypatch.setattr(cli.tracing, "tracer_from_environment", choosing)

    assert collect(tmp_path, OFFLINE, replay_client) == 0
    assert chosen == [tracing.NO_TRACER]


@pytest.fixture
def unreachable_langfuse(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[Tracer]]:
    """Langfuse's keys set, for an address where nothing answers. Yields the tracers made."""
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-unreachable-cli")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-unreachable-cli")
    monkeypatch.setenv("LANGFUSE_HOST", "http://127.0.0.1:9")
    made: list[Tracer] = []
    real = tracing.tracer_from_environment

    def choosing() -> Tracer:
        made.append(real())
        return made[-1]

    monkeypatch.setattr(cli.tracing, "tracer_from_environment", choosing)
    yield made
    from invoice_collector.langfuse_tracer import LangfuseTracer

    for tracer in made:
        if isinstance(tracer, LangfuseTracer):
            tracer.close(1.0)


def test_a_langfuse_that_cannot_be_reached_never_fails_a_run(
    tmp_path: Path, replay_client: ReplayClient, unreachable_langfuse: list[Tracer]
) -> None:
    started = time.monotonic()

    exit_code = collect(tmp_path, BY_CLAUDE, replay_client)

    assert exit_code == 0
    assert type(unreachable_langfuse[0]).__name__ == "LangfuseTracer"
    # The run waits for Langfuse a few seconds at most before it ends.
    assert time.monotonic() - started < tracing.FLUSH_SECONDS + 15
    ledger = Ledger(tmp_path / "out" / "ledger.sqlite")
    [run] = ledger.runs()
    ledger.close()
    assert run.finished_at is not None

