"""Extraction by sending the PDF to Claude."""

import base64
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Literal

import anthropic
from pydantic import BaseModel, Field, ValidationError

from invoice_collector.domain import Extraction
from invoice_collector.extractor import NO_HINTS, ExtractionFailed, Hints, NotABillingDocument
from invoice_collector.metering import NOT_METERED, Meter

DEFAULT_MODEL = "claude-haiku-4-5"

PROMPT = """Extract the billing fields from this document, which a SaaS vendor sent to a company.

The total is the final amount including tax. The invoice date is the date printed on the \
document, not a billing period and not a due date. If the document is not an invoice, a receipt \
or a credit note, say so. Report low confidence if any field had to be guessed.

Give the subtotal and the tax only where the document states them. Leave them empty where \
it does not: do not work them out."""


class _Fields(BaseModel):
    document_type: Literal["invoice", "receipt", "credit_note", "not_a_billing_document"]
    vendor: str = Field(
        description="Short brand name of the vendor, e.g. 'Slack', not the legal entity"
    )
    invoice_date: str = Field(description="Date printed on the document, as YYYY-MM-DD")
    total: str = Field(
        description="Total including tax, digits and decimal point only, e.g. '1250.00'"
    )
    currency: str = Field(description="ISO 4217 code, e.g. 'USD'")
    confidence: Literal["high", "medium", "low"]
    doubts: str = Field(description="Anything ambiguous about these fields, or an empty string")
    subtotal: str = Field(
        default="", description="Amount before tax as stated on the document, or an empty string"
    )
    tax: str = Field(default="", description="Tax as stated on the document, or an empty string")


def _amount(text: str) -> Decimal:
    amount = Decimal(text.replace(",", ""))
    if not amount.is_finite():
        raise ValueError(f"{text!r} is not an amount")
    return amount


class ClaudeExtractor:
    def __init__(
        self, client: anthropic.Anthropic, model: str = DEFAULT_MODEL, meter: Meter = NOT_METERED
    ) -> None:
        self._client = client
        self._model = model
        self._meter = meter

    def extract(self, pdf: bytes, hints: Hints = NO_HINTS) -> Extraction:
        # The model reads the document alone: names from the email are not trusted.
        try:
            response = self._client.messages.parse(
                model=self._model,
                max_tokens=1024,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "document",
                                "source": {
                                    "type": "base64",
                                    "media_type": "application/pdf",
                                    "data": base64.standard_b64encode(pdf).decode(),
                                },
                            },
                            {"type": "text", "text": PROMPT},
                        ],
                    }
                ],
                output_format=_Fields,
            )
        except anthropic.APIConnectionError as error:
            raise ExtractionFailed("could not reach the model") from error
        except anthropic.APIStatusError as error:
            raise ExtractionFailed(f"the model returned HTTP {error.status_code}") from error
        except ValidationError as error:
            raise ExtractionFailed("the answer of the model did not fit the fields") from error
        self._meter.record(self._model, response.usage.input_tokens, response.usage.output_tokens)

        fields = response.parsed_output
        if response.stop_reason != "end_turn" or fields is None:
            raise ExtractionFailed(f"the model stopped early: {response.stop_reason}")
        if fields.document_type == "not_a_billing_document":
            raise NotABillingDocument(fields.doubts or "the model found no billing document")

        vendor = fields.vendor.strip()
        currency = fields.currency.strip().upper()
        if not vendor:
            raise ExtractionFailed("the model returned no vendor")
        if not (len(currency) == 3 and currency.isascii() and currency.isalpha()):
            raise ExtractionFailed(f"the model returned no currency code: {fields.currency!r}")
        try:
            return Extraction(
                document_type=fields.document_type,
                vendor=vendor,
                invoice_date=date.fromisoformat(fields.invoice_date),
                total=_amount(fields.total),
                currency=currency,
                confidence=fields.confidence,
                doubts=fields.doubts,
                subtotal=_amount(fields.subtotal) if fields.subtotal.strip() else None,
                tax=_amount(fields.tax) if fields.tax.strip() else None,
                by=self._model,
            )
        except (ValueError, InvalidOperation) as error:
            raise ExtractionFailed(f"the model returned an unusable value: {error}") from error
