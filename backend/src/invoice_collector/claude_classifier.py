"""Classification by Claude, from the words of the email."""

from typing import Literal

import anthropic
from pydantic import BaseModel, Field, ValidationError

from invoice_collector.classifier import ClassificationFailed, text_of
from invoice_collector.domain import Classification, Email

DEFAULT_MODEL = "claude-haiku-4-5"

PROMPT = """Classify this email, which arrived in a company mailbox.

invoice: a vendor requests payment.
receipt: a vendor confirms a payment was taken.
credit_note: a vendor records money returned.
payment_failed: a payment could not be taken. No money moved.
renewal_reminder: a subscription will renew in future. No money moved.
not_billing: anything else, including newsletters, promotions and product updates.

Choose by what the email is, not by whether it mentions a price.

From: {sender}
Subject: {subject}
Attachments: {attachments}

{body}"""


class _Answer(BaseModel):
    kind: Literal[
        "invoice", "receipt", "credit_note", "payment_failed", "renewal_reminder", "not_billing"
    ]
    vendor: str = Field(
        description="Short brand name of the vendor, e.g. 'Slack'. Empty if not a billing email"
    )
    confidence: Literal["high", "medium", "low"]


class ClaudeClassifier:
    def __init__(self, client: anthropic.Anthropic, model: str = DEFAULT_MODEL) -> None:
        self._client = client
        self._model = model

    def classify(self, email: Email) -> Classification:
        prompt = PROMPT.format(
            sender=email.sender,
            subject=email.subject,
            attachments=", ".join(a.filename for a in email.attachments) or "none",
            body=text_of(email),
        )
        try:
            response = self._client.messages.parse(
                model=self._model,
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}],
                output_format=_Answer,
            )
        except anthropic.APIConnectionError as error:
            raise ClassificationFailed("could not reach the model") from error
        except anthropic.APIStatusError as error:
            raise ClassificationFailed(f"the model returned HTTP {error.status_code}") from error
        except ValidationError as error:
            raise ClassificationFailed("the answer of the model did not fit the kinds") from error

        answer = response.parsed_output
        if response.stop_reason != "end_turn" or answer is None:
            raise ClassificationFailed(f"the model stopped early: {response.stop_reason}")
        return Classification(
            answer.kind, answer.vendor.strip() or None, answer.confidence, by=self._model
        )
