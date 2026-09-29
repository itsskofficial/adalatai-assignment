---
status: accepted
---

# Model calls are traced in Langfuse, which is sent metadata and answers, not email or PDFs

Every call to a model can be traced in Langfuse: extraction, escalated extraction, classification, vendor matching and Ask your invoices. A trace gives the model, its input and output tokens, the cost, the time taken, and what the call was about. Eval runs are recorded as experiments, and corrections made in review as scores on the trace that produced the wrong value. This is the first time the tool sends anything about a company's billing to a service other than the model providers, so what is sent is decided here.

## What is sent

| | Sent by default | Sent only with `INVOICE_COLLECTOR_TRACE_CONTENT=1` | Never sent |
|---|---|---|---|
| A model call | The step, the model, input and output tokens, cost, start and end time, and an error's kind and HTTP status | The full input: the prompt, which holds the email's sender, subject and text, the PDF (to Langfuse's media store), and a question as the person typed it | A secret, an API key or a stored sign-in; an error's message, which may quote the email |
| What it was about | The run (as the session), the collection month, the source account, the email's message id, the invoice format, the billing document's content hash | | |
| What the model answered | The fields it read or the choice it made: vendor, invoice date, total, currency, document type, confidence, the kind of email, the chosen query and its parameters | | The model's free-text note on a reading, and its reason for declining a question |
| A review decision | For each field, 1 if the person confirmed it and 0 if they corrected it, and the corrected fields | | Who made the decision |
| An eval run | Each golden case's key, labels and invoice format, its right answer, the candidate's answer, its tokens, cost and time, and a score for each field | The case's email subject and text, or the question | |

The fields read are sent because without them a trace cannot show what a wrong answer was, and they are what a person sees in the summary. The email body and the PDF are not: they are a company's finance correspondence, often with bank details and addresses, and a trace is useful without them. A person debugging one document can turn content on, run it again, and turn it off.

The tool, not Langfuse, decides what is sent. Each model's adapter reports its call through the tool's own `Tracer` interface, and asks for the full input only when the tracer says it sends content. Langfuse is given a tracer provider of its own that exports only the spans the tool made, so an instrumented library could not add the body of a request behind the tool's back.

Text that came from an email and reaches Langfuse is data there as everywhere (ADR 0012). The history page links to a trace; it never shows what the trace holds.

## Considered Options

- **Instrument the Anthropic SDK with OpenTelemetry**: rejected. It records whole requests and responses, so the PDF and the email body would be sent unless every attribute were masked after the fact, and Jev has no instrumentation at all.
- **Send everything, and rely on Langfuse's masking**: rejected. A mask that misses a field sends finance data to a third party, and it cannot be recalled once sent.
- **Send only counts, no fields read**: rejected. A trace could then say that a reading cost a cent but not that it read the total wrongly, which is what a correction scores.
- **Metadata and answers by default, full content as an explicit setting**: chosen.

## Consequences

Tracing is optional. With no `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY`, the tracer does nothing and nothing is sent. A failure to reach Langfuse is a warning, never a failed run: a command waits at most a few seconds for its traces to be sent before it exits.

Only `langfuse_tracer.py` imports Langfuse. Replacing it, or self-hosting Langfuse so nothing leaves the company's own machines, changes no other module.

Each step of a document's history that called a model keeps the id of its trace and a link to it. A run that makes the same step again with a new trace does not add a step to the history.

The measured cost per billing document comes from these traces: `invoice-collector cost` reads a run's calls back from Langfuse. See `docs/research/cost-and-latency.md`.
