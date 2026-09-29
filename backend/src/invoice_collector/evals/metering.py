"""Token usage and cost of each call to a model.

The classifiers, extractors and matchers do not return the usage a response reports, so it is
read from the HTTP response itself, by a hook on the HTTP client each candidate is given. The
usage is added to whatever measurement is open on the calling thread.
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

Provider = Literal["anthropic", "jev", "none"]

# Per million tokens, input then output, from docs/cost-and-latency.md.
CLAUDE_PRICES_PER_MILLION: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}
# From docs/research/jev-api.md: input tokens are priced, output tokens are free.
JEV_PRICE_PER_MILLION_INPUT_TOKENS = 0.042


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


def cost_usd(provider: Provider, model: str, input_tokens: int, output_tokens: int) -> float:
    if provider == "anthropic":
        input_price, output_price = CLAUDE_PRICES_PER_MILLION[model]
        return (input_tokens * input_price + output_tokens * output_price) / 1_000_000
    if provider == "jev":
        return input_tokens * JEV_PRICE_PER_MILLION_INPUT_TOKENS / 1_000_000
    return 0.0


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
