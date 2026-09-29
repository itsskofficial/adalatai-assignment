"""What a run's model calls cost, measured from their traces. See ADR 0017.

Each call a run makes is traced with its tokens, cost and time, under the run as its
session. This reads them back for one run and sets them beside the billing documents of
its collection month, giving the measured cost per billing document that
docs/research/cost-and-latency.md otherwise assumes. `invoice-collector cost` prints it.

The run also records in the ledger the calls, tokens and cost of each model it called, as
its meter counted them (see metering.py). Each call gives the meter and the tracer the same
count of tokens, and both are priced from the one table in metering.py, so where both exist
they are set side by side and should agree. They differ when traces were lost, or when the
prices changed between the run and the reading.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from statistics import median

from invoice_collector.domain import CollectionMonth, ModelUsage
from invoice_collector.ledger import Ledger
from invoice_collector.tracing import MeasuredCall


@dataclass(frozen=True)
class StepCost:
    """The calls of one step, such as extraction, and what they took."""

    step: str
    models: tuple[str, ...]
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    # Calls whose cost was not recorded: a model with no price.
    unpriced: int
    median_seconds: float | None


@dataclass(frozen=True)
class TracedModel:
    """The traced calls to one model that answered, and what they cost. A call that failed
    is traced, but reports no tokens and is not metered, so it is not counted here."""

    model: str
    calls: int
    input_tokens: int
    output_tokens: int
    # None when any of the calls has no recorded cost.
    cost_usd: float | None


# How far apart the ledger's cost and the traces' may be and still agree: a trace keeps
# its call's cost as a float.
_SAME_COST_USD = 1e-6


@dataclass(frozen=True)
class Comparison:
    """One model's calls as the ledger recorded them and as they were traced. A side is
    None when it has no call to the model."""

    model: str
    recorded: ModelUsage | None
    traced: TracedModel | None

    @property
    def agrees(self) -> bool:
        recorded, traced = self.recorded, self.traced
        if recorded is None or traced is None:
            return False
        if (recorded.calls, recorded.input_tokens, recorded.output_tokens) != (
            traced.calls,
            traced.input_tokens,
            traced.output_tokens,
        ):
            return False
        if recorded.cost_usd is None or traced.cost_usd is None:
            return recorded.cost_usd is None and traced.cost_usd is None
        return abs(float(recorded.cost_usd) - traced.cost_usd) < _SAME_COST_USD


@dataclass(frozen=True)
class RunCost:
    run_id: int
    month: CollectionMonth
    # The distinct billing documents of the month after the run, collected or held.
    billing_documents: int
    steps: tuple[StepCost, ...]
    # Each model's calls in the ledger beside its traced calls. None when the ledger has no
    # cost for the run: one made before runs were metered.
    comparisons: tuple[Comparison, ...] | None = None

    @property
    def cost_usd(self) -> float:
        return sum(step.cost_usd for step in self.steps)

    @property
    def cost_per_document(self) -> float | None:
        return self.cost_usd / self.billing_documents if self.billing_documents else None


# The steps in the order a document meets them.
_ORDER = ("classification", "extraction", "escalated extraction", "vendor matching")


def _step(step: str, calls: Sequence[MeasuredCall]) -> StepCost:
    seconds = [c.seconds for c in calls if c.seconds is not None]
    return StepCost(
        step=step,
        models=tuple(sorted({c.model for c in calls if c.model})),
        calls=len(calls),
        input_tokens=sum(c.input_tokens or 0 for c in calls),
        output_tokens=sum(c.output_tokens or 0 for c in calls),
        cost_usd=sum(c.cost_usd or 0.0 for c in calls),
        unpriced=sum(c.cost_usd is None for c in calls),
        median_seconds=median(seconds) if seconds else None,
    )


def _traced_models(measured: Sequence[MeasuredCall]) -> dict[str, TracedModel]:
    by_model: dict[str, list[MeasuredCall]] = {}
    for call in measured:
        if call.model and (call.input_tokens is not None or call.output_tokens is not None):
            by_model.setdefault(call.model, []).append(call)
    return {
        model: TracedModel(
            model=model,
            calls=len(calls),
            input_tokens=sum(c.input_tokens or 0 for c in calls),
            output_tokens=sum(c.output_tokens or 0 for c in calls),
            cost_usd=(
                None
                if any(c.cost_usd is None for c in calls)
                else sum(c.cost_usd or 0.0 for c in calls)
            ),
        )
        for model, calls in by_model.items()
    }


def _compared(
    recorded: Sequence[ModelUsage], measured: Sequence[MeasuredCall]
) -> tuple[Comparison, ...]:
    in_ledger = {usage.model: usage for usage in recorded}
    traced = _traced_models(measured)
    return tuple(
        Comparison(model, in_ledger.get(model), traced.get(model))
        for model in sorted(in_ledger.keys() | traced.keys())
    )


def run_cost(
    ledger: Ledger,
    run_id: int,
    month: CollectionMonth,
    measured: Sequence[MeasuredCall],
    recorded: Sequence[ModelUsage] | None = None,
) -> RunCost:
    """The run's cost from its traces and, when recorded is given, each model's calls as
    the ledger recorded them beside it."""
    documents = {d.content_hash for d in ledger.documents(month)}
    documents |= {d.content_hash for d in ledger.pending(month)}
    by_step: dict[str, list[MeasuredCall]] = {}
    for call in measured:
        by_step.setdefault(call.step, []).append(call)
    order = sorted(by_step, key=lambda s: (_ORDER.index(s) if s in _ORDER else len(_ORDER), s))
    return RunCost(
        run_id,
        month,
        len(documents),
        tuple(_step(s, by_step[s]) for s in order),
        _compared(recorded, measured) if recorded is not None else None,
    )


def _pair(ledger: str | None, traces: str | None) -> str:
    return f"{ledger or 'none'} / {traces or 'none'}"


def _money(cost: float | None) -> str:
    return f"${cost:.5f}" if cost is not None else "unknown"


def comparison_lines(cost: RunCost) -> list[str]:
    """Each model's calls in the ledger beside its traces, and whether they agree."""
    if cost.comparisons is None:
        return ["The ledger has no cost for this run, so there is nothing to compare."]
    rows = [
        "The run's calls as the ledger recorded them, beside the traces (ledger / traces):",
        "",
        "| Model | Calls | Input tokens | Output tokens | Cost |",
        "|---|---|---|---|---|",
    ]
    for c in cost.comparisons:
        r, t = c.recorded, c.traced
        rows.append(
            f"| {c.model} "
            f"| {_pair(r and f'{r.calls:,}', t and f'{t.calls:,}')} "
            f"| {_pair(r and f'{r.input_tokens:,}', t and f'{t.input_tokens:,}')} "
            f"| {_pair(r and f'{r.output_tokens:,}', t and f'{t.output_tokens:,}')} "
            f"| {_pair(r and _money(_float(r)), t and _money(t.cost_usd))} |"
        )
    differ = [c.model for c in cost.comparisons if not c.agrees]
    rows.append("")
    if differ:
        rows.append(
            f"The ledger and the traces differ for {', '.join(differ)}: traces may have been "
            "lost, or the prices changed since the run."
        )
    else:
        rows.append("The ledger and the traces agree.")
    return rows


def _float(usage: ModelUsage) -> float | None:
    return float(usage.cost_usd) if usage.cost_usd is not None else None


def lines(cost: RunCost) -> list[str]:
    """The measurement as a table in Markdown, to go into the cost and latency document."""
    rows = [
        f"Run {cost.run_id}, collection month {cost.month}: {cost.billing_documents} billing "
        "documents, collected or held.",
        "",
        "| Step | Model | Calls | Input tokens | Output tokens | Cost | Median seconds |",
        "|---|---|---|---|---|---|---|",
    ]
    for step in cost.steps:
        seconds = (
            f"{step.median_seconds:.2f}" if step.median_seconds is not None else "not recorded"
        )
        priced = f"${step.cost_usd:.5f}" + (f" ({step.unpriced} unpriced)" if step.unpriced else "")
        rows.append(
            f"| {step.step} | {', '.join(step.models) or 'not recorded'} | {step.calls} | "
            f"{step.input_tokens:,} | {step.output_tokens:,} | {priced} | {seconds} |"
        )
    per_document = cost.cost_per_document
    rows += [
        f"| **Total** | | {sum(s.calls for s in cost.steps)} | | | **${cost.cost_usd:.5f}** | |",
        "",
        "Measured cost per billing document: "
        + (f"**${per_document:.5f}**" if per_document is not None else "none were collected"),
        "",
        *comparison_lines(cost),
    ]
    return rows
