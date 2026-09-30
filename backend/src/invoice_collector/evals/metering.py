"""Token usage and cost of each call to a model, as the eval measures them.

The eval reads the usage a response reports from the HTTP response itself, by a hook on the
HTTP client each candidate is given, and adds it to whatever measurement is open on the calling
thread, since it scores one answer at a time. A run meters its calls per model across threads
instead, with invoice_collector.metering, whose table of prices both use.
"""

import json
import threading
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal, cast

import anthropic
import httpx
import httpx2

from invoice_collector.metering import cost_usd as run_cost_usd

Provider = Literal["anthropic", "jev", "none"]


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


def cost_usd(provider: Provider, model: str, input_tokens: int, output_tokens: int) -> float:
    """What a call cost, priced from the table a run's cost is worked out from."""
    if provider == "none":
        return 0.0
    cost = run_cost_usd(model, input_tokens, output_tokens)
    if cost is None:
        raise KeyError(f"no price is known for {model}")
    return float(cost)


_local = threading.local()


@contextmanager
def measuring() -> Generator[Usage]:
    """Adds the usage of every response received on this thread, until the block ends."""
    usage = Usage()
    previous = cast(Usage | None, getattr(_local, "usage", None))
    _local.usage = usage
    try:
        yield usage
    finally:
        _local.usage = previous


def record_usage(body: bytes) -> None:
    """Adds the usage a response body reports to the open measurement, if there is one."""
    usage = cast(Usage | None, getattr(_local, "usage", None))
    if usage is None:
        return
    try:
        data: Any = json.loads(body)
    except ValueError:
        return
    if not isinstance(data, dict):
        return
    reported: Any = cast(dict[str, Any], data).get("usage")
    if not isinstance(reported, dict):
        return
    fields = cast(dict[str, Any], reported)
    input_tokens = fields.get("input_tokens")
    output_tokens = fields.get("output_tokens")
    usage.input_tokens += input_tokens if isinstance(input_tokens, int) else 0
    usage.output_tokens += output_tokens if isinstance(output_tokens, int) else 0


def _on_anthropic_response(response: httpx.Response) -> None:
    if response.status_code == 200:
        response.read()
        record_usage(response.content)


def _on_jev_response(response: httpx2.Response) -> None:
    if response.status_code == 200:
        response.read()
        record_usage(response.content)


def metered_anthropic(
    api_key: str | None = None, base_url: str | None = None, max_retries: int = 2
) -> anthropic.Anthropic:
    """A client for Claude whose responses' usage is recorded."""
    return anthropic.Anthropic(
        api_key=api_key,
        base_url=base_url,
        max_retries=max_retries,
        http_client=anthropic.DefaultHttpxClient(
            event_hooks={"response": [_on_anthropic_response]}
        ),
    )


def metered_jev_http_client(timeout: float) -> httpx2.Client:
    """An HTTP client for Jev whose responses' usage is recorded."""
    return httpx2.Client(timeout=timeout, event_hooks={"response": [_on_jev_response]})
