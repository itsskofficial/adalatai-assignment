"""The Langfuse tracer, sending to a local server that stands in for Langfuse.

The server answers each of Langfuse's endpoints the tracer uses in the shape its API
documents, and keeps what it was sent, so the tests read exactly what would reach Langfuse.
Nothing reaches Langfuse itself. See ADR 0017.
"""

import base64
import json
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import count
from pathlib import Path
from typing import Any

import pytest
from conftest import ReplayClient
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

from invoice_collector import tracing
from invoice_collector.claude_extractor import PROMPT, ClaudeExtractor
from invoice_collector.langfuse_tracer import LangfuseTracer
from invoice_collector.tracing import (
    Experiment,
    ExperimentItem,
    Finished,
    Score,
    TraceContext,
)

RECORDED = Path(__file__).parent / "recorded"
INVOICE: dict[str, Any] = json.loads((RECORDED / "claude_slack_invoice.json").read_text("utf-8"))
PDF = (RECORDED / "slack_invoice.pdf").read_bytes()
NOW = "2026-09-29T00:00:00.000Z"
ABOUT = TraceContext(
    run_id=3,
    collection_month="2026-08",
    source_account="ops@nyayalabs.example",
    message_id="m-slack-0803",
    invoice_format="attachment",
    document="d" * 64,
)
_keys = count()


@dataclass
class Received:
    """What the stand-in for Langfuse was sent."""

    spans: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    raw_spans: list[bytes] = field(default_factory=list[bytes])
    ingested: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    requests: list[tuple[str, dict[str, Any]]] = field(
        default_factory=list[tuple[str, dict[str, Any]]]
    )

    def scores(self) -> list[dict[str, Any]]:
        return [e["body"] for e in self.ingested if e["type"] == "score-create"]

    def posted(self, path: str) -> list[dict[str, Any]]:
        return [body for at, body in self.requests if at == path]


def _value(value: Any) -> Any:
    kind = value.WhichOneof("value")
    if kind == "array_value":
        return [_value(v) for v in value.array_value.values]
    return getattr(value, kind) if kind else None


def _spans(body: bytes) -> list[dict[str, Any]]:
    request = ExportTraceServiceRequest.FromString(body)
    return [
        {
            "name": span.name,
            "trace_id": span.trace_id.hex(),
            "attributes": {a.key: _value(a.value) for a in span.attributes},
        }
        for resource in request.resource_spans
        for scoped in resource.scope_spans
        for span in scoped.spans
    ]


def _answer(path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    if path == "/api/public/ingestion":
        return 207, {"successes": [{"id": e["id"], "status": 201} for e in body["batch"]]}
    if path == "/api/public/v2/datasets":
        return 200, {
            "id": "dataset-1",
            "name": body["name"],
            "description": body.get("description"),
            "metadata": None,
            "projectId": "project-1",
            "createdAt": NOW,
            "updatedAt": NOW,
        }
    if path == "/api/public/dataset-items":
        return 200, {
            "id": body["id"],
            "status": "ACTIVE",
            "input": body.get("input"),
            "expectedOutput": body.get("expectedOutput"),
            "metadata": body.get("metadata"),
            "sourceTraceId": None,
            "sourceObservationId": None,
            "datasetId": "dataset-1",
            "datasetName": body["datasetName"],
            "createdAt": NOW,
            "updatedAt": NOW,
            "mediaReferences": [],
        }
    if path == "/api/public/dataset-run-items":
        return 200, {
            "id": f"run-item-{body['datasetItemId']}",
            "datasetRunId": "dataset-run-1",
            "datasetRunName": body["runName"],
            "datasetItemId": body["datasetItemId"],
            "traceId": body["traceId"],
            "observationId": body.get("observationId"),
            "createdAt": NOW,
            "updatedAt": NOW,
        }
    return 404, {"message": "not found"}


StartLangfuse = Callable[[], tuple[str, Received]]

# Two generations of run 3, as Langfuse's API gives them back.
OBSERVATIONS: dict[str, Any] = {
    "data": [
        {
            "id": f"obs-{n}",
            "traceId": f"{n}" * 32,
            "startTime": NOW,
            "endTime": NOW,
            "projectId": "project-1",
            "parentObservationId": None,
            "type": "GENERATION",
            "name": name,
            "model": model,
            "usageDetails": {"input": tokens, "output": 50, "total": tokens + 50},
            "totalCost": cost,
            "latency": 2.5,
        }
        for n, name, model, tokens, cost in (
            (1, "classification", "jev-latest", 212, 0.0000089),
            (2, "extraction", "claude-haiku-4-5", 2365, 0.00265),
        )
    ],
    "meta": {"cursor": None},
}


@pytest.fixture
def langfuse_server() -> Iterator[StartLangfuse]:
    servers: list[ThreadingHTTPServer] = []

    def start() -> tuple[str, Received]:
        received = Received()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                pass

            def _reply(self, status: int, payload: bytes, kind: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self) -> None:
                path, _, query = self.path.partition("?")
                received.requests.append((path, {"query": query}))
                if path == "/api/public/v2/observations":
                    self._reply(200, json.dumps(OBSERVATIONS).encode(), "application/json")
                    return
                self._reply(404, b"{}", "application/json")

            def do_POST(self) -> None:
                sent = self.rfile.read(int(self.headers["Content-Length"]))
                path = self.path.split("?")[0]
                if path == "/api/public/otel/v1/traces":
                    received.raw_spans.append(sent)
                    received.spans.extend(_spans(sent))
                    self._reply(200, b"", "application/x-protobuf")
                    return
                body = json.loads(sent)
                received.requests.append((path, body))
                if path == "/api/public/ingestion":
                    received.ingested.extend(body["batch"])
                status, answer = _answer(path, body)
                self._reply(status, json.dumps(answer).encode(), "application/json")

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{server.server_port}", received

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


@pytest.fixture
def tracer_at() -> Iterator[Callable[..., LangfuseTracer]]:
    """A Langfuse tracer for a host, with keys of its own: the SDK keeps one client a key."""
    made: list[LangfuseTracer] = []

    def make(host: str, *, sends_content: bool = False) -> LangfuseTracer:
        number = next(_keys)
        tracer = LangfuseTracer(
            public_key=f"pk-lf-test-{number}",
            secret_key=f"sk-lf-test-{number}",
            host=host,
            sends_content=sends_content,
        )
        made.append(tracer)
        return tracer

    yield make
    for tracer in made:
        tracer.close(2.0)


def test_a_model_call_reaches_langfuse_as_a_generation_with_what_it_was_about(
    langfuse_server: StartLangfuse, tracer_at: Callable[..., LangfuseTracer]
) -> None:
    host, received = langfuse_server()
    tracer = tracer_at(host)

    call = tracer.start("extraction", "claude-haiku-4-5", ABOUT, None)
    call.finish(Finished(1.5, 2365, 57, 0.0026, {"vendor": "Slack", "total": "652.50"}))
    assert tracer.flush(10) is None

    [span] = received.spans
    assert call.link is not None
    assert span["trace_id"] == call.link.trace_id
    assert call.link.url == f"{host}/trace/{call.link.trace_id}"
    attributes = span["attributes"]
    assert span["name"] == "extraction"
    assert attributes["langfuse.observation.type"] == "generation"
    assert attributes["langfuse.observation.model.name"] == "claude-haiku-4-5"
    assert json.loads(attributes["langfuse.observation.usage_details"]) == {
        "input": 2365,
        "output": 57,
    }
    assert json.loads(attributes["langfuse.observation.cost_details"]) == {"total": 0.0026}
    assert json.loads(attributes["langfuse.observation.output"]) == {
        "vendor": "Slack",
        "total": "652.50",
    }
    assert attributes["session.id"] == "run-3"
    assert attributes["langfuse.trace.name"] == "extraction"
    assert set(attributes["langfuse.trace.tags"]) == {
        "extraction",
        "collection-month:2026-08",
        "invoice-format:attachment",
    }
    assert attributes["langfuse.trace.metadata.billing_document"] == "d" * 64
    assert attributes["langfuse.trace.metadata.message_id"] == "m-slack-0803"
    assert "langfuse.observation.input" not in attributes


def test_by_default_neither_the_pdf_nor_the_prompt_reaches_langfuse(
    langfuse_server: StartLangfuse,
    tracer_at: Callable[..., LangfuseTracer],
    replay_client: ReplayClient,
) -> None:
    host, received = langfuse_server()
    tracer = tracer_at(host)

    ClaudeExtractor(replay_client(200, INVOICE), tracer=tracer).extract(PDF)
    tracer.flush(10)

    [sent] = received.raw_spans
    assert base64.standard_b64encode(PDF)[:200] not in sent
    assert PDF[:200] not in sent
    assert PROMPT[:60].encode() not in sent
    assert b"Slack" in sent


def test_with_content_sent_the_prompt_is_in_the_trace(
    langfuse_server: StartLangfuse,
    tracer_at: Callable[..., LangfuseTracer],
    replay_client: ReplayClient,
) -> None:
    host, received = langfuse_server()
    tracer = tracer_at(host, sends_content=True)

    with tracing.scope(message_id="m-1"):
        ClaudeExtractor(replay_client(200, INVOICE), tracer=tracer).extract(PDF)
    tracer.flush(10)

    [span] = received.spans
    sent = json.loads(span["attributes"]["langfuse.observation.input"])
    assert sent["text"] == PROMPT
    # The PDF goes to Langfuse's media store, and the trace holds a reference to it.
    assert sent["pdf"].startswith("@@@langfuseMedia:type=application/pdf")


def test_a_failed_call_is_an_error_in_langfuse(
    langfuse_server: StartLangfuse, tracer_at: Callable[..., LangfuseTracer]
) -> None:
    host, received = langfuse_server()
    tracer = tracer_at(host)

    tracer.start("classification", "jev-latest", TraceContext(), None).finish(
        Finished(0.2, error="TypeSafeAPIConnectionError")
    )
    tracer.flush(10)

    [span] = received.spans
    assert span["attributes"]["langfuse.observation.level"] == "ERROR"
    assert span["attributes"]["langfuse.observation.status_message"] == (
        "TypeSafeAPIConnectionError"
    )


def test_scores_reach_langfuse_on_the_trace(
    langfuse_server: StartLangfuse, tracer_at: Callable[..., LangfuseTracer]
) -> None:
    host, received = langfuse_server()
    tracer = tracer_at(host)
    trace_id = "a" * 32

    tracer.score(
        trace_id,
        [
            Score("review.total", 0, "boolean", "corrected in review to 650.00"),
            Score("output", '{"total": "650.00"}', "correction"),
        ],
    )
    tracer.flush(10)

    assert [(s["traceId"], s["name"], s["value"], s["dataType"]) for s in received.scores()] == [
        (trace_id, "review.total", 0, "BOOLEAN"),
        (trace_id, "output", '{"total": "650.00"}', "CORRECTION"),
    ]


def test_an_eval_run_is_an_experiment_against_a_dataset_with_per_field_scores(
    langfuse_server: StartLangfuse, tracer_at: Callable[..., LangfuseTracer]
) -> None:
    host, received = langfuse_server()
    tracer = tracer_at(host)
    items = [
        ExperimentItem(
            key="ops/slack.eml",
            input={"case": "ops/slack.eml", "labels": []},
            expected={"vendor": "Slack", "total": "652.50"},
            output={"vendor": "Slack", "total": "652.50"},
            scores={"vendor": 1.0, "total": 1.0},
            input_tokens=2300,
            output_tokens=60,
            seconds=2.1,
            cost_usd=0.0026,
        ),
        ExperimentItem(
            key="ops/aws.eml",
            input={"case": "ops/aws.eml", "labels": ["multi-page"]},
            expected={"vendor": "AWS", "total": "90.00"},
            output={"vendor": "AWS", "total": "9.00"},
            scores={"vendor": 1.0, "total": 0.0},
        ),
    ]

    tracer.experiment(
        Experiment(
            dataset="invoice-collector/standard/extraction",
            run_name="claude-haiku abc123 2026-09-29T00:00:00Z",
            description="extraction by claude-haiku (claude-haiku-4-5)",
            model="claude-haiku-4-5",
            provider="anthropic",
            metadata={"job": "extraction"},
            items=items,
        )
    )
    tracer.flush(10)

    [dataset] = received.posted("/api/public/v2/datasets")
    assert dataset["name"] == "invoice-collector/standard/extraction"
    posted_items = received.posted("/api/public/dataset-items")
    assert [i["expectedOutput"]["vendor"] for i in posted_items] == ["Slack", "AWS"]
    run_items = received.posted("/api/public/dataset-run-items")
    assert len(run_items) == 2
    assert {r["runName"] for r in run_items} == {"claude-haiku abc123 2026-09-29T00:00:00Z"}
    scores = {(s["traceId"], s["name"]): s["value"] for s in received.scores()}
    by_item = {r["datasetItemId"]: r["traceId"] for r in run_items}
    aws = by_item[posted_items[1]["id"]]
    assert scores[(aws, "vendor")] == 1.0
    assert scores[(aws, "total")] == 0.0
    generations = [s for s in received.spans if s["name"] == "claude-haiku-4-5"]
    assert {
        json.loads(g["attributes"]["langfuse.observation.usage_details"])["input"]
        for g in generations
    } == {2300, 0}


def test_a_langfuse_that_cannot_be_reached_costs_seconds_and_raises_nothing(
    tracer_at: Callable[..., LangfuseTracer],
) -> None:
    tracer = tracer_at("http://127.0.0.1:9")
    started = time.monotonic()

    call = tracer.start("extraction", "claude-haiku-4-5", ABOUT, None)
    call.finish(Finished(1.0, 10, 1, 0.0, {}))
    tracer.score("b" * 32, [Score("review.vendor", 1, "boolean")])
    tracer.flush(2.0)

    assert time.monotonic() - started < 10


def test_a_run_is_measured_from_the_generations_langfuse_recorded(
    langfuse_server: StartLangfuse, tracer_at: Callable[..., LangfuseTracer]
) -> None:
    host, received = langfuse_server()
    tracer = tracer_at(host)

    measured = tracer.measured(3)

    assert measured is not None
    assert [(m.step, m.model, m.input_tokens, m.cost_usd) for m in measured] == [
        ("classification", "jev-latest", 212, 0.0000089),
        ("extraction", "claude-haiku-4-5", 2365, 0.00265),
    ]
    [(_, asked)] = [r for r in received.requests if r[0] == "/api/public/v2/observations"]
    assert "sessionId=run-3" in asked["query"]
    assert "type=GENERATION" in asked["query"]


def test_a_run_that_cannot_be_read_back_is_not_measured(
    tracer_at: Callable[..., LangfuseTracer],
) -> None:
    assert tracer_at("http://127.0.0.1:9").measured(3) is None
