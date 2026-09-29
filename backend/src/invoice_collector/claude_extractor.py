"""Extraction by sending the PDF to Claude."""

import base64
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from invoice_collector.domain import Extraction
from invoice_collector.extractor import ExtractionFailed, NotABillingDocument

DEFAULT_MODEL = "claude-haiku-4-5"

PROMPT = """Extract the billing fields from this document, which a SaaS vendor sent to a company.

The total is the final amount including tax. The invoice date is the date printed on the \
document, not a billing period and not a due date. If the document is not an invoice, a receipt \
or a credit note, say so. Report low confidence if any field had to be guessed."""


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


class ClaudeExtractor:
    def __init__(self, client: anthropic.Anthropic, model: str = DEFAULT_MODEL) -> None:
        self._client = client
        self._model = model

    def extract(self, pdf: bytes) -> Extraction:
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

        fields = response.parsed_output
        if response.stop_reason != "end_turn" or fields is None:
            raise ExtractionFailed(f"the model stopped early: {response.stop_reason}")
        if fields.document_type == "not_a_billing_document":
            raise NotABillingDocument(fields.doubts or "the model found no billing document")

        try:
            return Extraction(
                document_type=fields.document_type,
                vendor=fields.vendor.strip(),
                invoice_date=date.fromisoformat(fields.invoice_date),
                total=Decimal(fields.total.replace(",", "")),
                currency=fields.currency.strip().upper(),
                confidence=fields.confidence,
                doubts=fields.doubts,
            )
        except (ValueError, InvalidOperation) as error:
            raise ExtractionFailed(f"the model returned an unusable value: {error}") from error
