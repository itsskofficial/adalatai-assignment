# Jev API: what the documentation says

Researched on 2026-09-29 for ticket #29 and ADR 0009.

Official documentation **was found**: <https://docs.typesafe.ai> (index at <https://docs.typesafe.ai/llms.txt>). An official Python SDK exists and is what the code uses.

## How far to trust this file

- **Read directly**: the source of the official SDK, `typesafe-sdk` 0.7.2, as installed in `backend/.venv`. Its wire models are generated from the API's OpenAPI schema, so field names and types below marked "SDK source" are exact.
- **Read through a summarising fetch tool**: the documentation and legal pages. Facts are reliable; wording given in quotation marks may not be letter-perfect. Re-read the legal pages by hand before Jev is made a default.
- **No live call was made.** The documentation and the SDK source agree, so none was needed. The recorded responses in `backend/tests/recorded/jev_*.json` are written by hand to the documented shape, not captured from Jev.

## Facts

| Fact | Value | Source |
| --- | --- | --- |
| Base URL | `https://api.typesafe.ai` | <https://docs.typesafe.ai/api.md>; SDK source `constants.py` (`DEFAULT_BASE_URL`) |
| Endpoint | `POST /v1/systemone` | <https://docs.typesafe.ai/api.md>, <https://docs.typesafe.ai/concepts/system-one.md> |
| Authentication | `Authorization: Bearer <API_KEY>` | <https://docs.typesafe.ai/api.md>; SDK source `_core/transport.py` |
| Content type | `application/json` | <https://docs.typesafe.ai/api.md> |
| Model name | `jev-latest`, an alias of `jev-1.13.0`. `jev-preview` is currently the same model | <https://docs.typesafe.ai/models.md> |
| Question types | `choice`, `score` (2 to 10 levels), `noul` | <https://docs.typesafe.ai/api.md>, <https://docs.typesafe.ai/primitives.md> |
| Options in a Choice | up to 255. The documentation advises an explicit "other" or "none of the above" option | <https://docs.typesafe.ai/primitives/choice.md> |
| Several questions in one request | yes: `questions` is a map of name to question, answered in parallel against the same state | <https://docs.typesafe.ai/api.md>, <https://docs.typesafe.ai/patterns/fan-out.md> |
| Input | text only: a string, a JSON object, or an array of text | <https://docs.typesafe.ai/concepts/system-one.md> |
| Size of a request | 64k tokens in total; the state plus the longest question may use 32k | <https://docs.typesafe.ai/models.md> |
| Rate limits | 250,000 tokens per second and 1,200 requests per minute, which "may adjust dynamically" | <https://docs.typesafe.ai/models.md> |
| Price | $0.042 per million input tokens; output tokens free | <https://docs.typesafe.ai/models.md> |
| Languages | English is best; others are supported with lower accuracy | <https://docs.typesafe.ai/models.md> |
| Python SDK | `typesafe-sdk` (import `typesafe_sdk`), sync `TypeSafeClient` and async `AsyncTypeSafeClient` | <https://docs.typesafe.ai/sdk/python.md> |
| SDK's environment variable for the key | `TYPESAFE_API_KEY`. This project reads `JEV_API_KEY` instead and passes the key in | <https://docs.typesafe.ai/sdk/python.md> |
| SDK defaults | 10 second timeout for each attempt; 2 retries with backoff on connection failure, 408, 429 and 5xx; 30 second budget over all attempts | SDK source `constants.py`, `_core/retry.py` |

## Request: one Choice

From <https://docs.typesafe.ai/api.md> and <https://docs.typesafe.ai/primitives/choice.md>; field types from SDK source `_schemas/models.py`.

```json
{
  "state": "Invoice INV-2041 from Slack Technologies for the Pro plan.",
  "model": "jev-latest",
  "questions": {
    "vendor": {
      "type": "choice",
      "instructions": "Which vendor issued this billing document?",
      "criteria": {
        "Slack": null,
        "Notion": null,
        "none of these": "The document was issued by a vendor that is not among the other options."
      }
    }
  }
}
```

- `state`: string, JSON object or array.
- `instructions`: string, object or array; optional.
- `criteria`: a map of option name to a description. A description may be `null`, in which case the option is read by its name alone. Names and descriptions are both sent to the model.

## Request: several questions

The same body with more entries in `questions`. Each entry carries its own `type`.

```json
{
  "state": {"subject": "Your payment failed", "body": "We could not process your payment."},
  "model": "jev-latest",
  "questions": {
    "kind": {"type": "choice", "instructions": "What kind of email is this?", "criteria": {"invoice": "A vendor requests payment.", "not_billing": "Anything else."}},
    "is_urgent": {"type": "noul", "instructions": "Does this convey urgency?"}
  }
}
```

## Response

From <https://docs.typesafe.ai/api.md>; the Choice answer's fields from SDK source `_schemas/models.py` (`ChoiceAnswer`, `Usage`).

```json
{
  "model": "jev-1.13.0",
  "answers": {
    "kind": {
      "type": "choice",
      "choice": "payment_failed",
      "confidence": 0.94,
      "probabilities": {"invoice": 0.01, "payment_failed": 0.96, "not_billing": 0.03}
    }
  },
  "usage": {"input_tokens": 212, "output_tokens": 8}
}
```

- `answers.<name>.choice`: the option with the highest probability.
- `answers.<name>.probabilities`: one probability for each option, summing to about 1. **The probability of the chosen option is `probabilities[choice]`.** This is what the code stores in `Classification.probability` and maps to the project's confidence.
- `answers.<name>.confidence`: a separate statistic from 0 to 1, computed from how concentrated the probabilities are. It is not the probability of the answer (<https://docs.typesafe.ai/confidence.md>). The code does not use it. The exact formula is not documented beyond an approximation for three options; the eval may wish to compare it with the probability.
- A Noul answer is `{"type": "noul", "noul": 0.95}`.

## Errors

| Status | Meaning | Source |
| --- | --- | --- |
| 401 | missing or invalid API key | <https://docs.typesafe.ai/api.md> |
| 422 | the request failed validation | <https://docs.typesafe.ai/api.md> |
| 429 | rate limited; retry with exponential backoff | <https://docs.typesafe.ai/api.md> |
| 529 | overloaded; retry with exponential backoff | <https://docs.typesafe.ai/api.md> |
| 400, 403, 404, 5xx | also given their own exception classes by the SDK | <https://docs.typesafe.ai/sdk/python/api/exceptions.md>; SDK source `_core/errors.py` |

Error body, as documented: `{"error": "Invalid request body"}`. The SDK also accepts `{"error": {"message": ...}}`, `{"message": ...}` and a `detail` list of validation entries with `loc` and `msg`, which suggests the body's shape varies by status (SDK source `_core/errors.py`). The code relies only on the status code.

Headers on rate limiting: the SDK reads `retry-after-ms` and `Retry-After`. The request id is returned in `x-typesafe-request-id` (SDK source).

## Data retention and privacy

| Fact | Source |
| --- | --- |
| Typesafe commits not to train or fine-tune models on inputs: "We will not train or fine tune any artificial intelligence or machine learning models on your prompts or other Input." | <https://typesafe.ai/legal/privacy-policy> |
| Inputs are not disclosed to third parties other than Typesafe's service providers | <https://typesafe.ai/legal/privacy-policy> |
| **No fixed retention period is stated.** Personal data is kept "for as long as reasonably necessary" (privacy policy) and "for as long as necessary taking into account the purpose of the Processing" (data processing agreement) | <https://typesafe.ai/legal/privacy-policy>, <https://typesafe.ai/legal/data-processing> |
| Deletion is on request | <https://typesafe.ai/legal/privacy-policy> |
| Zero data retention is offered to enterprise customers only, by arrangement with sales@typesafe.ai. It is not the default | <https://docs.typesafe.ai/legal.md> |
| Transfers rely on the EU standard contractual clauses (module 2) and the UK addendum. Subprocessors are listed at <https://trust.typesafe.ai/subprocessors> | <https://typesafe.ai/legal/data-processing> |

Consequence for this project: with an ordinary account, the text of emails sent to Jev is kept by Typesafe for a period it does not state. This must be said in the README before Jev is made a default (ADR 0009).

## Found only in third-party sources

| Fact | Source | Standing |
| --- | --- | --- |
| Released on 15 September 2026, in early access | <https://www.netlify.com/changelog/typesafe-jev-ai-gateway/> (search result summary) | not confirmed in official documentation that was read |
| Response times of 70 to 500 ms | same | not confirmed; the eval measures time per call itself |
| A budget of "roughly 32,000 tokens" shared by state and questions | same | the official figure is 64k in total with 32k for the state plus the longest question |
| Price of $0.04 per million input tokens | same | the official figure is $0.042 |

## Not found

- Whether request and response bodies are logged, and for how long.
- Where data is stored geographically.
- How `confidence` is computed in general.
- Whether the rate limits differ by account tier.
- Whether access is still by waitlist, as ADR 0009 says.
- The error body for each status code, beyond the one documented example.

## What the code assumes

All in one section at the top of `backend/src/invoice_collector/jev_classifier.py`: the base URL, the model name, the limit of 255 options, the timeout and retries, and a limit of 24,000 characters on the whole request: the email's details, the question, the options and the text, which is cut to what room is left. Tokens are not counted; the limit is low enough to stay under 32k tokens even in a script that takes more than a token for each character. Everything else about the wire format is inside the SDK.
