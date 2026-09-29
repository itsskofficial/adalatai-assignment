"""Classification by Jev, from the words of the email.

Jev is a classification model from Typesafe AI. It does not write text: it is given state and a
question with a fixed set of options, and returns the chosen option with a probability for each.
"""

from collections.abc import Mapping

from typesafe_sdk import (
    Choice,
    RetryPolicy,
    TypeSafeAPIConnectionError,
    TypeSafeAPIError,
    TypeSafeAPIResponseValidationError,
    TypeSafeClient,
    TypeSafeError,
)

from invoice_collector.classifier import ClassificationFailed, text_of, vendor_from_sender
from invoice_collector.domain import Classification, Confidence, Email, EmailKind

# --- What this module takes from Jev's documentation (docs/research/jev-api.md) ---------------
# Every fact about Jev that the code relies on is in this section or inside the official SDK.
DEFAULT_BASE_URL = "https://api.typesafe.ai"  # the SDK appends /v1/systemone
DEFAULT_MODEL = "jev-latest"
MAX_CHOICE_OPTIONS = 255
# A request may carry 64k tokens, of which the state and the longest question may use 32k.
# Tokens cannot be counted here, so the whole request is held to a number of characters
# that stays below that even in a script that takes a token or more for each character.
MAX_REQUEST_CHARACTERS = 24_000
# The SDK's own defaults: 10 seconds for each attempt, and two further attempts after a
# connection failure or HTTP 408, 429 or 5xx.
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_RETRIES = 2
# -----------------------------------------------------------------------------------------------

# The probability of the chosen kind, as the project's confidence. To be tuned by the eval.
HIGH_CONFIDENCE_AT = 0.90
MEDIUM_CONFIDENCE_AT = 0.70

INSTRUCTIONS = (
    "What kind of email is this, which arrived in a company mailbox? "
    "Choose by what the email is, not by whether it mentions a price."
)
KINDS: dict[EmailKind, str] = {
    "invoice": "A vendor requests payment.",
    "receipt": "A vendor confirms a payment was taken.",
    "credit_note": "A vendor records money returned.",
    "payment_failed": "A payment could not be taken. No money moved.",
    "renewal_reminder": "A subscription will renew in future. No money moved.",
    "not_billing": "Anything else, including newsletters, promotions and product updates.",
}

_KIND_NAMED: dict[str, EmailKind] = {kind: kind for kind in KINDS}

JevState = str | dict[str, str | list[str]]


def cut_to_fit(text: str, *rest_of_request: str | None) -> str:
    """The start of text that fits in a request beside everything else it carries."""
    taken = sum(len(part) for part in rest_of_request if part)
    return text[: max(MAX_REQUEST_CHARACTERS - taken, 0)]


class JevFailed(Exception):
    """Jev gave no usable answer."""


def confidence_of(probability: float) -> Confidence:
    if probability >= HIGH_CONFIDENCE_AT:
        return "high"
    if probability >= MEDIUM_CONFIDENCE_AT:
        return "medium"
    return "low"


def jev_client(
    api_key: str,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> TypeSafeClient:
    return TypeSafeClient(
        api_key=api_key,
        base_url=base_url,
        timeout=timeout,
        retry=RetryPolicy(max_retries=max_retries),
    )


def ask_choice(
    client: TypeSafeClient,
    model: str,
    state: JevState,
    name: str,
    instructions: str,
    options: Mapping[str, str | None],
) -> tuple[str, float]:
    """The option Jev chose and its probability. Raises JevFailed."""
    try:
        # The SDK's recursive JSON type is partly unknown to pyright.
        response = client.system_one(  # pyright: ignore[reportUnknownMemberType]
            state, {name: Choice(instructions=instructions, criteria=options)}, model=model
        )
    except TypeSafeAPIResponseValidationError as error:
        raise JevFailed(f"Jev returned a malformed response at {error.field_path!r}") from error
    except TypeSafeAPIConnectionError as error:
        raise JevFailed("could not reach Jev") from error
    except TypeSafeAPIError as error:
        raise JevFailed(f"Jev returned HTTP {error.status}") from error
    except TypeSafeError as error:
        raise JevFailed(f"Jev could not be asked: {error}") from error

    answer = response.choices.get(name)
    if answer is None:
        raise JevFailed(f"Jev returned a malformed response: no choice named {name!r}")
    if answer.choice not in options:
        raise JevFailed(f"Jev's answer is not one of the options offered: {answer.choice}")
    probability = answer.probabilities.get(answer.choice)
    if probability is None:
        raise JevFailed("Jev returned a malformed response: no probability for its choice")
    return answer.choice, probability


class JevClassifier:
    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        self._client = jev_client(api_key, base_url, timeout, max_retries)
        self._model = model

    def classify(self, email: Email) -> Classification:
        attachments = [a.filename for a in email.attachments]
        state: JevState = {
            "from": email.sender,
            "subject": email.subject,
            "attachments": attachments,
            "body": cut_to_fit(
                text_of(email),
                email.sender,
                email.subject,
                *attachments,
                INSTRUCTIONS,
                *KINDS,
                *KINDS.values(),
            ),
        }
        try:
            choice, probability = ask_choice(
                self._client, self._model, state, "kind", INSTRUCTIONS, dict(KINDS.items())
            )
        except JevFailed as failure:
            raise ClassificationFailed(str(failure)) from failure

        kind = _KIND_NAMED[choice]
        vendor = None if kind == "not_billing" else vendor_from_sender(email)
        return Classification(kind, vendor, confidence_of(probability), probability)
