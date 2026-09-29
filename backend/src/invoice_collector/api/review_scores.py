"""A review decision, as scores on the traces of the model calls it judged. See ADR 0017.

A person on the Review screen confirms or corrects each field a model read, or judges
that the email holds no billing document. Each field is scored on the trace of the call
that produced it: 1 when the person confirmed it, 0 when they corrected it, with the
corrected fields as Langfuse's own kind of score for a correction. The vendor is scored on
the vendor match's trace when a model matched it, since the match chose its spelling. A
judgement that the email holds no billing document scores its classification and reading
0. Only the fields and the decision are sent, never who made it.

The traces are found in the history of the billing document, where each step that called
a model keeps its trace. A document read before tracing, or with tracing off, has none,
and nothing is scored.
"""

import json
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from invoice_collector import tracing, trail
from invoice_collector.domain import Extraction

REVIEW_PREFIX = "review."
BILLING_DOCUMENT = f"{REVIEW_PREFIX}billing_document"
# The name Langfuse gives a correction of a call's output.
CORRECTION = "output"


@dataclass(frozen=True)
class Reviewed:
    """A billing document as a person decided it: confirmed is None when they judged that
    the email holds no billing document."""

    content_hash: str
    read: Extraction
    confirmed: Extraction | None


def _traces(ledger_path: Path, content_hash: str) -> dict[str, str]:
    """The trace of the latest step of each kind that called a model for the document."""
    with closing(sqlite3.connect(ledger_path)) as db:
        stored = trail.events_of(db, content_hash)
    traces: dict[str, str] = {}
    for each in stored:
        trace_id = each.event.details.get(trail.TRACE_ID)
        if isinstance(trace_id, str):
            traces[each.event.kind] = trace_id
    return traces


def scores_of(reviewed: Reviewed, traces: dict[str, str]) -> dict[str, list[tracing.Score]]:
    """The scores to record, by the trace each belongs to."""
    reading = traces.get(trail.READ_AGAIN) or traces.get(trail.READ)
    scored: dict[str, list[tracing.Score]] = {}
    if reviewed.confirmed is None:
        judged = tracing.Score(
            BILLING_DOCUMENT, 0, "boolean", "a person judged it not to be a billing document"
        )
        for trace_id in dict.fromkeys(t for t in (traces.get(trail.CLASSIFIED), reading) if t):
            scored.setdefault(trace_id, []).append(judged)
        return scored

    before, after = trail.fields_read(reviewed.read), trail.fields_read(reviewed.confirmed)
    corrected = {name: after[name] for name in before if before[name] != after[name]}
    for name in before:
        trace_id = (traces.get(trail.MATCHED) or reading) if name == "vendor" else reading
        if trace_id is None:
            continue
        right = name not in corrected
        comment = "confirmed in review" if right else f"corrected in review to {after[name]}"
        score = tracing.Score(f"{REVIEW_PREFIX}{name}", 1 if right else 0, "boolean", comment)
        scored.setdefault(trace_id, []).append(score)
    if corrected and reading is not None:
        correction = tracing.Score(CORRECTION, json.dumps(after, sort_keys=True), "correction")
        scored.setdefault(reading, []).append(correction)
    return scored


def record(tracer: tracing.Tracer, ledger_path: Path, documents: Sequence[Reviewed]) -> None:
    """Scores each trace the decision judged. Never raises: a failure is a warning."""
    if isinstance(tracer, tracing.NoTracer):
        return
    for reviewed in documents:
        try:
            traces = _traces(ledger_path, reviewed.content_hash)
        except sqlite3.Error as failure:
            tracing.warn_once(f"the review could not be scored: {failure}")
            continue
        for trace_id, scores in scores_of(reviewed, traces).items():
            tracing.record_scores(tracer, trace_id, scores)
