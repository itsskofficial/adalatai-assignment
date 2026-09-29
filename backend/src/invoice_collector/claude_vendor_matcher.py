"""Vendor matching by Claude, from the text of the billing document."""

from collections.abc import Sequence
from typing import Literal

import anthropic
from pydantic import BaseModel, Field, ValidationError

from invoice_collector import tracing
from invoice_collector.metering import NOT_METERED, Meter
from invoice_collector.vendor_matcher import NONE_OF_THESE, VendorMatch, VendorMatchFailed

DEFAULT_MODEL = "claude-haiku-4-5"

PROMPT = """Which vendor issued this billing document?

Choose one of the vendors listed, written exactly as it is listed. A vendor's legal name and \
its brand name are the same vendor. If the document was issued by a vendor that is not \
listed, answer "{none}". Choose by who issued the document, not by who is mentioned in it.

Vendors:
{vendors}

Document:
{text}"""

_PROBABILITY = {"high": 0.95, "medium": 0.75, "low": 0.5}


class _Answer(BaseModel):
    vendor: str = Field(description="One of the vendors exactly as listed, or 'none of these'")
    confidence: Literal["high", "medium", "low"]


def _answered(response: object) -> dict[str, object] | None:
    parsed = getattr(response, "parsed_output", None)
    return parsed.model_dump() if isinstance(parsed, _Answer) else None


class ClaudeVendorMatcher:
    def __init__(
        self,
        client: anthropic.Anthropic,
        model: str = DEFAULT_MODEL,
        meter: Meter = NOT_METERED,
        tracer: tracing.Tracer = tracing.NO_TRACER,
    ) -> None:
        self._client = client
        self._model = model
        self._meter = meter
        self._tracer = tracer

    def match(self, text: str, expected_vendors: Sequence[str]) -> VendorMatch:
        vendors = list(dict.fromkeys(expected_vendors))
        if not vendors:
            return VendorMatch(None)

        prompt = PROMPT.format(
            none=NONE_OF_THESE, vendors="\n".join(f"- {v}" for v in vendors), text=text
        )
        try:
            response = tracing.traced(
                self._tracer,
                tracing.VENDOR_MATCHING,
                self._model,
                "anthropic",
                self._client.messages.parse,
                output=_answered,
                content=lambda: tracing.Content(text=prompt),
                meter=self._meter,
            )(
                model=self._model,
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}],
                output_format=_Answer,
            )
        except anthropic.APIConnectionError as error:
            raise VendorMatchFailed("could not reach the model") from error
        except anthropic.APIStatusError as error:
            raise VendorMatchFailed(f"the model returned HTTP {error.status_code}") from error
        except ValidationError as error:
            raise VendorMatchFailed("the answer of the model did not fit") from error

        answer = response.parsed_output
        if response.stop_reason != "end_turn" or answer is None:
            raise VendorMatchFailed(f"the model stopped early: {response.stop_reason}")

        chosen = answer.vendor.strip()
        probability = _PROBABILITY[answer.confidence]
        if chosen.casefold() == NONE_OF_THESE:
            return VendorMatch(None, probability, self._model)
        by_spelling = {vendor.casefold(): vendor for vendor in vendors}
        if chosen.casefold() not in by_spelling:
            raise VendorMatchFailed(f"the model chose a vendor that was not offered: {chosen!r}")
        return VendorMatch(by_spelling[chosen.casefold()], probability, self._model)
