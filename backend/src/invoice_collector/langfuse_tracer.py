"""Traces model calls, review corrections and eval runs in Langfuse. See ADR 0017.

The only module that imports Langfuse. It uses the Langfuse Python SDK as its version 4
documents itself: a generation observation per call, started with an explicit trace id so
the trace can be linked from the audit trail before it is sent; propagate_attributes for
what the trace is about (the run as its session, the collection month and invoice format
as tags); create_score for corrections; datasets and run_experiment for eval runs.

The SDK sends in the background. Nothing here raises into the pipeline: a failure is a
warning, and flushing waits at most the time it is given. Langfuse is given a tracer
provider of its own, which exports only the spans made through the SDK here, so nothing
another library records reaches Langfuse.
"""

import atexit
import hashlib
import threading
from collections.abc import Callable, Mapping, Sequence
from typing import Any, cast

from langfuse import Evaluation, Langfuse, LangfuseMedia, propagate_attributes
from langfuse.api import MediaContentType
from langfuse.span_filter import is_langfuse_span
from opentelemetry.sdk.trace import TracerProvider

from invoice_collector import tracing
from invoice_collector.tracing import (
    Content,
    Experiment,
    ExperimentItem,
    Finished,
    MeasuredCall,
    OpenCall,
    Score,
    TraceContext,
    TraceLink,
    failure_of,
    warn_once,
)

# How long a request to Langfuse may take, in seconds. The SDK's own default.
REQUEST_TIMEOUT_SECONDS = 5
# How long recording an eval run as an experiment may take before the eval goes on.
EXPERIMENT_SECONDS = 120.0
# How many observations are read back from Langfuse at a time, its largest page.
MEASURED_PAGE = 1000

_SCORE_TYPES = {"number": "NUMERIC", "boolean": "BOOLEAN", "correction": "CORRECTION"}


def trace_url(host: str, trace_id: str) -> str:
    """A link to a trace. Langfuse redirects /trace/<id> to the trace in its project."""
    return f"{host.rstrip('/')}/trace/{trace_id}"


def _bounded(work: Callable[[], None], timeout: float) -> str | None:
    """Does the work on a thread of its own, waiting at most timeout seconds for it.

    A warning when it failed or did not finish in time. The thread does not keep the
    process alive.
    """
    failures: list[BaseException] = []

    def run() -> None:
        try:
            work()
        except BaseException as failure:
            failures.append(failure)

    worker = threading.Thread(target=run, name="langfuse-send", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        return f"Langfuse did not answer within {timeout:g} seconds"
    if failures:
        return f"Langfuse could not be reached: {failure_of(failures[0])}"
    return None


def _tags(context: TraceContext, step: str) -> list[str]:
    tags = [step]
    if context.collection_month is not None:
        tags.append(f"collection-month:{context.collection_month}")
    if context.invoice_format is not None:
        tags.append(f"invoice-format:{context.invoice_format}")
    return tags


def _metadata(context: TraceContext) -> dict[str, str]:
    """What the call was about, each as the short text Langfuse keeps with the trace."""
    about = {
        "run_id": context.run_id,
        "collection_month": context.collection_month,
        "source_account": context.source_account,
        "message_id": context.message_id,
        "invoice_format": context.invoice_format,
        "billing_document": context.document,
    }
    return {name: str(value) for name, value in about.items() if value is not None}


def _input(content: Content | None) -> dict[str, Any] | None:
    """The full input of a call, only when content is sent. The PDF goes as a media file."""
    if content is None:
        return None
    given: dict[str, Any] = {}
    if content.text is not None:
        given["text"] = content.text
    if content.pdf is not None:
        given["pdf"] = LangfuseMedia(
            content_bytes=content.pdf, content_type=MediaContentType.APPLICATION_PDF
        )
    return given


class _Call:
    def __init__(self, generation: Any, link: TraceLink) -> None:
        self._generation = generation
        self.link: TraceLink | None = link

    def finish(self, finished: Finished) -> None:
        try:
            usage: dict[str, int] = {}
            if finished.input_tokens is not None:
                usage["input"] = finished.input_tokens
            if finished.output_tokens is not None:
                usage["output"] = finished.output_tokens
            self._generation.update(
                output=(
                    dict(finished.output)
                    if finished.output is not None
                    else {"error": finished.error}
                    if finished.error
                    else None
                ),
                usage_details=usage or None,
                cost_details=(
                    {"total": finished.cost_usd} if finished.cost_usd is not None else None
                ),
                level="ERROR" if finished.error else None,
                status_message=finished.error,
            )
            self._generation.end()
        except Exception as failure:
            warn_once(f"a model call could not be traced in Langfuse: {failure_of(failure)}")


class _NotTraced:
    link: TraceLink | None = None

    def finish(self, finished: Finished) -> None:
        pass


class LangfuseTracer:
    """Sends traces, scores and experiments to one Langfuse project."""

    def __init__(
        self,
        *,
        public_key: str,
        secret_key: str,
        host: str,
        sends_content: bool = False,
        flush_interval: float | None = None,
    ) -> None:
        self.sends_content = sends_content
        self._host = host.rstrip("/")
        self._client = Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            base_url=self._host,
            timeout=REQUEST_TIMEOUT_SECONDS,
            flush_interval=flush_interval,
            # Its own provider, which exits without waiting to send: flush waits instead,
            # for a time it is given.
            tracer_provider=TracerProvider(shutdown_on_exit=False),
            should_export_span=is_langfuse_span,
        )
        # Registered after the SDK's own handler, so it runs first: the SDK's handler is
        # removed by shutdown, which would otherwise wait on Langfuse without a limit.
        atexit.register(self._at_exit)

    def start(
        self, step: str, model: str, context: TraceContext, content: Content | None
    ) -> OpenCall:
        try:
            trace_id = Langfuse.create_trace_id()
            with propagate_attributes(
                session_id=f"run-{context.run_id}" if context.run_id is not None else None,
                tags=_tags(context, step),
                metadata=_metadata(context),
                trace_name=step,
            ):
                generation = self._client.start_observation(
                    trace_context={"trace_id": trace_id},
                    name=step,
                    as_type="generation",
                    model=model,
                    input=_input(content),
                    metadata={**_metadata(context), "content_sent": content is not None},
                )
            return _Call(generation, TraceLink(trace_id, trace_url(self._host, trace_id)))
        except Exception as failure:
            warn_once(f"a model call could not be traced in Langfuse: {failure_of(failure)}")
            return _NotTraced()

    def score(self, trace_id: str, scores: Sequence[Score]) -> None:
        for score in scores:
            try:
                self._client.create_score(
                    name=score.name,
                    value=score.value,
                    trace_id=trace_id,
                    data_type=cast(Any, _SCORE_TYPES[score.type]),
                    comment=score.comment,
                )
            except Exception as failure:
                warn_once(f"a score could not be sent to Langfuse: {failure_of(failure)}")

    def experiment(self, experiment: Experiment) -> None:
        warning = _bounded(lambda: self._experiment(experiment), EXPERIMENT_SECONDS)
        if warning is not None:
            warn_once(f"the eval run was not recorded as an experiment: {warning}")

    def _experiment(self, experiment: Experiment) -> None:
        client = self._client
        client.create_dataset(
            name=experiment.dataset,
            description="Golden cases of the invoice collector's offline eval",
        )
        ours: dict[str, ExperimentItem] = {}
        items: list[Any] = []
        for item in experiment.items:
            ours[item.key] = item
            items.append(
                client.create_dataset_item(
                    dataset_name=experiment.dataset,
                    id=_item_id(experiment.dataset, item.key),
                    input=dict(item.input),
                    expected_output=dict(item.expected),
                    metadata={"case": item.key},
                )
            )

        def answered(*, item: Any, **_: Any) -> Any:
            """The answer the eval already has for the case, with what it cost."""
            case = ours[_case_of(getattr(item, "metadata", None))]
            client.start_observation(
                name=experiment.model,
                as_type="generation",
                model=experiment.model,
                output=dict(case.output) if case.output is not None else None,
                usage_details={"input": case.input_tokens, "output": case.output_tokens},
                cost_details={"total": case.cost_usd} if case.cost_usd is not None else None,
                metadata={"cached": case.cached, "seconds": case.seconds},
                level="ERROR" if case.error else None,
                status_message=case.error,
            ).end()
            return dict(case.output) if case.output is not None else {"error": case.error}

        def scored(*, metadata: Any = None, **_: Any) -> list[Evaluation]:
            case = ours[_case_of(metadata)]
            return [Evaluation(name=name, value=value) for name, value in case.scores.items()]

        client.run_experiment(
            name=experiment.dataset,
            run_name=experiment.run_name,
            description=experiment.description,
            data=items,
            task=answered,
            evaluators=[scored],
            metadata=dict(experiment.metadata),
        )

    def flush(self, timeout: float) -> str | None:
        warning = _bounded(self._client.flush, timeout)
        return f"traces may not all have been sent: {warning}" if warning else None

    def measured(self, run_id: int) -> Sequence[MeasuredCall] | None:
        """Every generation of the run's session, read back through Langfuse's API, with
        the tokens, cost and latency Langfuse recorded. None when it cannot be read."""
        measured: list[MeasuredCall] = []
        cursor: str | None = None
        try:
            while True:
                page = self._client.api.observations.get_many(
                    session_id=f"run-{run_id}",
                    type="GENERATION",
                    fields="core,basic,model,usage,metrics",
                    limit=MEASURED_PAGE,
                    cursor=cursor,
                )
                for observation in page.data:
                    usage = observation.usage_details or {}
                    measured.append(
                        MeasuredCall(
                            step=observation.name or "",
                            model=observation.model,
                            input_tokens=usage.get("input"),
                            output_tokens=usage.get("output"),
                            cost_usd=observation.total_cost,
                            seconds=observation.latency,
                        )
                    )
                cursor = page.meta.cursor
                if not cursor or not page.data:
                    return measured
        except Exception as failure:
            warn_once(f"the run's calls could not be read from Langfuse: {failure_of(failure)}")
            return None

    def close(self, timeout: float = tracing.FLUSH_SECONDS) -> str | None:
        """Sends what is waiting and stops the SDK's threads, waiting at most timeout
        seconds. A warning when it could not. Done at exit if not before."""
        atexit.unregister(self._at_exit)
        warning = _bounded(self._client.shutdown, timeout)
        return f"traces may not all have been sent: {warning}" if warning else None

    def _at_exit(self) -> None:
        _bounded(self._client.shutdown, tracing.FLUSH_SECONDS)


def _item_id(dataset: str, key: str) -> str:
    """A dataset item's id, the same for the same case on every run, and unique across
    datasets as Langfuse requires."""
    return hashlib.sha256(f"{dataset}\n{key}".encode()).hexdigest()[:32]


def _case_of(metadata: object) -> str:
    if isinstance(metadata, Mapping):
        case = cast(Mapping[str, object], metadata).get("case")
        if isinstance(case, str):
            return case
    raise LookupError("the dataset item names no golden case")
