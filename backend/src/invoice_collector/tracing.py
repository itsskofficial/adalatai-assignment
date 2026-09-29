"""Tracing of the calls made to models, behind an interface. See ADR 0017.

Every call to a model can be traced: extraction, escalated extraction, classification,
vendor matching and Ask your invoices. A trace carries the model, its input and output
tokens, the cost, the time taken, and what the call was about: the run, the collection
month, the source account and message id of the email, the invoice format and the
billing document. Eval runs are recorded as experiments, and corrections made in review
as scores on the trace that produced the wrong value.

Core logic knows only this module. Whoever builds the pipeline chooses the tracer from the
environment with tracer_from_environment: Langfuse when its keys are present, otherwise
NO_TRACER, which traces nothing and sends nothing. langfuse_tracer.py is the only module
that imports Langfuse.

What is sent, by default, is metadata and what the model answered: never the email body,
the PDF, the prompt, a secret or a stored sign-in. The full input of each call is sent
only when INVOICE_COLLECTOR_TRACE_CONTENT is set to 1. A tracer never raises into the
pipeline: a failure to trace, or to reach the tracing service, is at most a warning.
"""

import os
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar, Token
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Protocol, Self

from invoice_collector import metering, trail

PUBLIC_KEY_VARIABLE = "LANGFUSE_PUBLIC_KEY"
SECRET_KEY_VARIABLE = "LANGFUSE_SECRET_KEY"
# Where Langfuse is. LANGFUSE_BASE_URL is the name the current Langfuse SDK reads;
# LANGFUSE_HOST, its older name, is read too.
HOST_VARIABLES = ("LANGFUSE_HOST", "LANGFUSE_BASE_URL")
DEFAULT_HOST = "https://cloud.langfuse.com"
# Set to 1 to send the full input of each call: the email's text, the PDF, the prompt.
TRACE_CONTENT_VARIABLE = "INVOICE_COLLECTOR_TRACE_CONTENT"
# How long a command waits for traces to be sent before it exits.
FLUSH_SECONDS = 5.0

Provider = Literal["anthropic", "jev"]
ScoreType = Literal["number", "boolean", "correction"]

# The name of each step whose calls are traced.
CLASSIFICATION = "classification"
EXTRACTION = "extraction"
ESCALATED_EXTRACTION = "escalated extraction"
VENDOR_MATCHING = "vendor matching"
QUESTION = "question"


@dataclass(frozen=True)
class TraceContext:
    """What a call to a model was about. Each is None when it is not known."""

    run_id: int | None = None
    collection_month: str | None = None
    source_account: str | None = None
    message_id: str | None = None
    invoice_format: str | None = None
    # The content hash of the billing document the call read or matched. See ADR 0013.
    document: str | None = None
    # The step, when the caller names it more exactly than the model's adapter can: an
    # extractor does not know that it is reading a document again.
    step: str | None = None


@dataclass(frozen=True)
class TraceLink:
    """Where the trace of one call can be found."""

    trace_id: str
    url: str | None = None

    def details(self) -> dict[str, str]:
        """As the step it belongs to records it in the history of a billing document."""
        details = {trail.TRACE_ID: self.trace_id}
        if self.url is not None:
            details[trail.TRACE_URL] = self.url
        return details


@dataclass(frozen=True)
class Content:
    """The full input of a call. Sent only when the tracer is told to send content."""

    text: str | None = None
    pdf: bytes | None = None


@dataclass(frozen=True)
class Finished:
    """How a call ended."""

    seconds: float
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    # What the model answered, as plain values. None when it gave no usable answer.
    output: Mapping[str, Any] | None = None
    # Why the call failed, when it did: the kind of failure, never a message that could
    # quote the email.
    error: str | None = None


@dataclass(frozen=True)
class Score:
    name: str
    value: float | str
    type: ScoreType = "number"
    comment: str | None = None


@dataclass(frozen=True)
class ExperimentItem:
    """One golden case as a candidate answered it, and its score for each field."""

    key: str
    # What identifies the case; its content only when content is sent.
    input: Mapping[str, Any]
    expected: Mapping[str, Any]
    output: Mapping[str, Any] | None
    # 1.0 for a field answered right, 0.0 for one answered wrong.
    scores: Mapping[str, float]
    error: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    cost_usd: float | None = None
    # The answer was read from the eval's cache: its tokens and time are the first call's.
    cached: bool = False


@dataclass(frozen=True)
class Experiment:
    """One candidate's run over one dataset: a golden set and a job, or the questions."""

    dataset: str
    run_name: str
    description: str
    model: str
    provider: str
    metadata: Mapping[str, str]
    items: Sequence[ExperimentItem]


@dataclass(frozen=True)
class MeasuredCall:
    """One call of a run as the tracing service recorded it. None where it recorded nothing."""

    step: str
    model: str | None
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    seconds: float | None


class OpenCall(Protocol):
    """A call to a model that has started."""

    @property
    def link(self) -> TraceLink | None: ...

    def finish(self, finished: Finished) -> None: ...


class Tracer(Protocol):
    """Records calls to models, scores and experiments. Never raises into its caller."""

    @property
    def sends_content(self) -> bool:
        """Whether the full input of each call is sent."""
        ...

    def start(
        self, step: str, model: str, context: TraceContext, content: Content | None
    ) -> OpenCall: ...

    def score(self, trace_id: str, scores: Sequence[Score]) -> None: ...

    def experiment(self, experiment: Experiment) -> None: ...

    def flush(self, timeout: float) -> str | None:
        """Sends what is waiting, taking at most timeout seconds. A warning when it could not."""
        ...

    def measured(self, run_id: int) -> Sequence[MeasuredCall] | None:
        """The model calls of a run, as they were traced. None when they cannot be known."""
        ...


class _NotTraced:
    link: TraceLink | None = None

    def finish(self, finished: Finished) -> None:
        pass


class NoTracer:
    """Traces nothing and sends nothing: the tracer when no Langfuse keys are present."""

    sends_content = False

    def start(
        self, step: str, model: str, context: TraceContext, content: Content | None
    ) -> OpenCall:
        return _NotTraced()

    def score(self, trace_id: str, scores: Sequence[Score]) -> None:
        pass

    def experiment(self, experiment: Experiment) -> None:
        pass

    def flush(self, timeout: float) -> str | None:
        return None

    def measured(self, run_id: int) -> Sequence[MeasuredCall] | None:
        return None


NO_TRACER: Tracer = NoTracer()


@dataclass
class FakeCall:
    """A call as the fake tracer recorded it."""

    step: str
    model: str
    context: TraceContext
    content: Content | None
    link: TraceLink
    finished: Finished | None = None

    def finish(self, finished: Finished) -> None:
        self.finished = finished


class TracingUnavailable(Exception):
    """What the fake tracer raises when it is told to fail."""


@dataclass
class FakeTracer:
    """Keeps what it is given, for tests. When failing, every method raises."""

    sends_content: bool = False
    failing: bool = False
    calls: list[FakeCall] = field(default_factory=list[FakeCall])
    scores: dict[str, list[Score]] = field(default_factory=dict[str, list[Score]])
    experiments: list[Experiment] = field(default_factory=list[Experiment])
    flushes: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _fail(self) -> None:
        if self.failing:
            raise TracingUnavailable("the tracing service cannot be reached")

    def start(
        self, step: str, model: str, context: TraceContext, content: Content | None
    ) -> OpenCall:
        self._fail()
        with self._lock:
            trace_id = f"{len(self.calls) + 1:032x}"
            call = FakeCall(
                step, model, context, content, TraceLink(trace_id, f"https://traces/{trace_id}")
            )
            self.calls.append(call)
        return call

    def score(self, trace_id: str, scores: Sequence[Score]) -> None:
        self._fail()
        with self._lock:
            self.scores.setdefault(trace_id, []).extend(scores)

    def experiment(self, experiment: Experiment) -> None:
        self._fail()
        with self._lock:
            self.experiments.append(experiment)

    def flush(self, timeout: float) -> str | None:
        self._fail()
        self.flushes += 1
        return None

    def measured(self, run_id: int) -> Sequence[MeasuredCall] | None:
        self._fail()
        return [
            MeasuredCall(
                call.step,
                call.model,
                call.finished.input_tokens,
                call.finished.output_tokens,
                call.finished.cost_usd,
                call.finished.seconds,
            )
            for call in self.calls
            if call.context.run_id == run_id and call.finished is not None
        ]


# --- What a call is about ------------------------------------------------------------------


class Scope:
    """What the calls made inside a scope are about, and the traces they made.

    Entered with `with`. A scope inside another adds to what the outer one says, and the
    traces made inside it are also the outer scope's.
    """

    def __init__(self, given: Mapping[str, str | int | None]) -> None:
        self._given = {name: value for name, value in given.items() if value is not None}
        self.context = TraceContext()
        self.links: list[TraceLink] = []
        self._outer: Scope | None = None
        self._token: Token[Scope | None] | None = None

    def __enter__(self) -> Self:
        self._outer = _current.get()
        outer = self._outer.context if self._outer is not None else TraceContext()
        self.context = replace(outer, **self._given)
        self._token = _current.set(self)
        return self

    def __exit__(self, *raised: object) -> None:
        if self._token is not None:
            _current.reset(self._token)
            self._token = None
        if self._outer is not None:
            self._outer.links.extend(self.links)

    @property
    def latest(self) -> TraceLink | None:
        """The trace of the last call made in the scope: the answer that was used, when
        one model stands behind another."""
        return self.links[-1] if self.links else None

    def details(self) -> dict[str, str]:
        """The latest trace, as the step it belongs to records it; empty when none."""
        return self.latest.details() if self.latest is not None else {}


_current: ContextVar[Scope | None] = ContextVar("invoice_collector_trace_scope", default=None)


def scope(
    *,
    run_id: int | None = None,
    collection_month: str | None = None,
    source_account: str | None = None,
    message_id: str | None = None,
    invoice_format: str | None = None,
    document: str | None = None,
    step: str | None = None,
) -> Scope:
    """Says what the calls made inside it are about, to be entered with `with`."""
    return Scope(
        {
            "run_id": run_id,
            "collection_month": collection_month,
            "source_account": source_account,
            "message_id": message_id,
            "invoice_format": invoice_format,
            "document": document,
            "step": step,
        }
    )


# --- Tracing a call ------------------------------------------------------------------------

_warned: set[str] = set()


def warn_once(message: str) -> None:
    """Prints a warning about tracing, once for each message."""
    if message in _warned:
        return
    _warned.add(message)
    print(f"Warning: {message}", file=sys.stderr)


def cost_usd(provider: Provider, model: str, input_tokens: int, output_tokens: int) -> float | None:
    """The cost of a call, from the one table of prices a run's cost is worked out from;
    None for a model with no price. The table is kept by model, so the provider is not
    needed to price it."""
    priced = metering.cost_usd(model, input_tokens, output_tokens)
    return float(priced) if priced is not None else None


def _tokens(usage: object, name: str) -> int | None:
    value = getattr(usage, name, None)
    return value if isinstance(value, int) else None


def failure_of(error: BaseException) -> str:
    """A failure as a trace names it: its kind and, for an HTTP error, its status."""
    status = getattr(error, "status_code", None) or getattr(error, "status", None)
    return f"{type(error).__name__} (HTTP {status})" if status else type(error).__name__


def traced[**P, R](
    tracer: Tracer,
    step: str,
    model: str,
    provider: Provider,
    send: Callable[P, R],
    *,
    output: Callable[[R], Mapping[str, Any] | None],
    content: Callable[[], Content] | None = None,
    meter: metering.Meter = metering.NOT_METERED,
) -> Callable[P, R]:
    """send, made to trace each call as a step of the current scope, and to report its
    tokens to meter.

    output gives what the model answered, as plain values, from its response. content
    gives the full input, and is asked for only when the tracer sends content. Whatever
    the tracer does, the call's result or its exception is what the caller gets. The
    tokens are read once from the response's usage, as Claude and Jev both report it, and
    the same count goes to the meter and to the trace, so the two cannot disagree. A call
    that raises reports nothing to the meter.
    """

    def call(*args: P.args, **kwargs: P.kwargs) -> R:
        current = _current.get()
        context = current.context if current is not None else TraceContext()
        opened: OpenCall | None = None
        try:
            given = content() if content is not None and tracer.sends_content else None
            opened = tracer.start(context.step or step, model, context, given)
        except Exception as failure:
            warn_once(f"a model call could not be traced: {failure_of(failure)}")
        started = time.perf_counter()
        try:
            result = send(*args, **kwargs)
        except Exception as error:
            failed = Finished(time.perf_counter() - started, error=failure_of(error))
            _finish(opened, failed, current)
            raise
        seconds = time.perf_counter() - started
        usage = getattr(result, "usage", None)
        in_tokens, out_tokens = _tokens(usage, "input_tokens"), _tokens(usage, "output_tokens")
        meter.record(model, in_tokens, out_tokens)
        try:
            answered = output(result)
        except Exception:
            answered = None
        cost = (
            cost_usd(provider, model, in_tokens or 0, out_tokens or 0)
            if in_tokens is not None or out_tokens is not None
            else None
        )
        _finish(opened, Finished(seconds, in_tokens, out_tokens, cost, answered), current)
        return result

    return call


def _finish(opened: OpenCall | None, finished: Finished, current: Scope | None) -> None:
    if opened is None:
        return
    try:
        opened.finish(finished)
        link = opened.link
    except Exception as failure:
        warn_once(f"a model call could not be traced: {failure_of(failure)}")
        return
    if link is not None and current is not None:
        current.links.append(link)


# --- Scores, experiments and flushing, never raising ---------------------------------------


def record_scores(tracer: Tracer, trace_id: str, scores: Sequence[Score]) -> None:
    if not scores:
        return
    try:
        tracer.score(trace_id, scores)
    except Exception as failure:
        warn_once(f"scores could not be traced: {failure_of(failure)}")


def record_experiment(tracer: Tracer, experiment: Experiment) -> None:
    try:
        tracer.experiment(experiment)
    except Exception as failure:
        warn_once(f"the eval could not be recorded as an experiment: {failure_of(failure)}")


def flush(tracer: Tracer, timeout: float = FLUSH_SECONDS) -> str | None:
    """Sends what is waiting before a command exits. A warning when it could not."""
    try:
        return tracer.flush(timeout)
    except Exception as failure:
        return f"traces may not have been sent: {failure_of(failure)}"


# --- Choosing the tracer -------------------------------------------------------------------


def tracer_from_environment(environ: Mapping[str, str] | None = None) -> Tracer:
    """Langfuse when both its keys are present; otherwise NO_TRACER, and nothing is sent.

    A Langfuse client that cannot be made is a warning, and nothing is traced.
    """
    env = os.environ if environ is None else environ
    public_key = env.get(PUBLIC_KEY_VARIABLE, "").strip()
    secret_key = env.get(SECRET_KEY_VARIABLE, "").strip()
    if not public_key or not secret_key:
        return NO_TRACER
    host = next((env[n].strip() for n in HOST_VARIABLES if env.get(n, "").strip()), DEFAULT_HOST)
    try:
        from invoice_collector.langfuse_tracer import LangfuseTracer

        return LangfuseTracer(
            public_key=public_key,
            secret_key=secret_key,
            host=host,
            sends_content=env.get(TRACE_CONTENT_VARIABLE, "").strip() == "1",
        )
    except Exception as failure:
        warn_once(f"Langfuse cannot be used, so nothing is traced: {failure_of(failure)}")
        return NO_TRACER
