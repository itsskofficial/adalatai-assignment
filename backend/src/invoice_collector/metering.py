"""What the models a run calls cost.

Each model adapter reports the tokens of every call it makes, and the model it asked, to the
meter it was given by whoever built the pipeline. A run's examinations may call models on
several threads at once, so the meter a run keeps is safe to use from any thread. The cost is
worked out from the one table of prices below, which the evals use too.
"""

import threading
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Protocol

from invoice_collector.domain import ModelUsage, total_cost

# US dollars per million tokens, input then output, for each model by the name it is asked
# for. Claude's from docs/research/cost-and-latency.md; Jev's from docs/research/jev-api.md,
# which prices input tokens only. A model that is not here has no known price.
PRICES_PER_MILLION_TOKENS: dict[str, tuple[Decimal, Decimal]] = {
    "claude-haiku-4-5": (Decimal("1.00"), Decimal("5.00")),
    "claude-sonnet-5-5": (Decimal("2.00"), Decimal("10.00")),
    "claude-opus-5-5": (Decimal("4.00"), Decimal("20.00")),
    "jev-latest": (Decimal("0.042"), Decimal("0")),
}


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> Decimal | None:
    """What a model's tokens cost, or None when the model's price is not known."""
    price = PRICES_PER_MILLION_TOKENS.get(model)
    if price is None:
        return None
    input_price, output_price = price
    return (input_tokens * input_price + output_tokens * output_price) / 1_000_000


class Meter(Protocol):
    def record(self, model: str, input_tokens: int | None, output_tokens: int | None) -> None:
        """Counts one call to a model and the tokens it reported. None is a count the
        response did not report."""
        ...


class NotMetered:
    """Counts nothing. What an adapter reports to when nobody asked what it costs."""

    def record(self, model: str, input_tokens: int | None, output_tokens: int | None) -> None:
        pass


NOT_METERED = NotMetered()


@dataclass(frozen=True)
class Call:
    model: str
    input_tokens: int | None
    output_tokens: int | None


class FakeMeter:
    """Keeps every call reported to it, in order, for a test to look at."""

    def __init__(self) -> None:
        self.calls: list[Call] = []

    def record(self, model: str, input_tokens: int | None, output_tokens: int | None) -> None:
        self.calls.append(Call(model, input_tokens, output_tokens))


@dataclass
class _Counted:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    # Calls whose response did not report its tokens, which leave the cost unknown.
    unreported: int = 0


class RunMeter:
    """Counts the calls of one run to each model. Safe to use from several threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counted: dict[str, _Counted] = {}

    def record(self, model: str, input_tokens: int | None, output_tokens: int | None) -> None:
        with self._lock:
            counted = self._counted.setdefault(model, _Counted())
            counted.calls += 1
            counted.input_tokens += input_tokens or 0
            counted.output_tokens += output_tokens or 0
            if input_tokens is None or output_tokens is None:
                counted.unreported += 1

    def usage(self) -> tuple[ModelUsage, ...]:
        """The calls, tokens and cost of each model called so far, by model name."""
        with self._lock:
            counted = sorted(self._counted.items())
        return tuple(
            ModelUsage(
                model=model,
                calls=c.calls,
                input_tokens=c.input_tokens,
                output_tokens=c.output_tokens,
                cost_usd=(
                    None if c.unreported else cost_usd(model, c.input_tokens, c.output_tokens)
                ),
            )
            for model, c in counted
        )


def describe_cost(models: Sequence[ModelUsage] | None) -> str:
    """What a run's model calls cost, in words: an amount, unknown, or not recorded."""
    if models is None:
        return "not recorded"
    if not models:
        return "$0 (no model was called)"
    unpriced = [m.model for m in models if m.cost_usd is None]
    if unpriced:
        return f"unknown: the cost of {', '.join(unpriced)} is not known"
    total = total_cost(models)
    assert total is not None
    return dollars(total)


def dollars(amount: Decimal) -> str:
    """An amount in US dollars, to the cent or, below that, to a hundredth of a cent, which
    a single call may cost: $0.42, $0.0133, under $0.0001."""
    if amount == 0:
        return "$0"
    shown = amount.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    if shown == 0:
        return "under $0.0001"
    text = f"{shown:.4f}"
    while text.endswith("0") and len(text.partition(".")[2]) > 2:
        text = text[:-1]
    return f"${text}"
