"""What a run's model calls cost, measured from their traces. See ADR 0017.

Each call a run makes is traced with its tokens, cost and time, under the run as its
session. This reads them back for one run and sets them beside the billing documents of
its collection month, giving the measured cost per billing document that
docs/research/cost-and-latency.md otherwise assumes. `invoice-collector cost` prints it.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from statistics import median

from invoice_collector.domain import CollectionMonth
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
class RunCost:
    run_id: int
    month: CollectionMonth
    # The distinct billing documents of the month after the run, collected or held.
    billing_documents: int
    steps: tuple[StepCost, ...]

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


def run_cost(
    ledger: Ledger, run_id: int, month: CollectionMonth, measured: Sequence[MeasuredCall]
) -> RunCost:
    documents = {d.content_hash for d in ledger.documents(month)}
    documents |= {d.content_hash for d in ledger.pending(month)}
    by_step: dict[str, list[MeasuredCall]] = {}
    for call in measured:
        by_step.setdefault(call.step, []).append(call)
    order = sorted(by_step, key=lambda s: (_ORDER.index(s) if s in _ORDER else len(_ORDER), s))
    return RunCost(run_id, month, len(documents), tuple(_step(s, by_step[s]) for s in order))


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
    ]
    return rows
