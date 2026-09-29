---
status: accepted
---

# Extraction starts on Claude Haiku 4.5 and escalates on low confidence

Extraction reads a document and copies out five fields. It is a reading task, not a reasoning task, so we start with the smallest current Claude model, Haiku 4.5, and escalate a billing document to Claude Sonnet 5.5 only when a cross-check fails or confidence is low. The offline eval, not this document, decides whether Haiku stays: if it falls below the accuracy threshold on the golden dataset, the default moves up a tier.

## Why Haiku 4.5

- **It fits the task.** The extraction spike sent one document of each invoice format to Haiku 4.5 and got all fields right on all three, including the two easy mistakes: it returned the brand name and not the legal entity, and the invoice date and not the billing period.
- **It has the two features the pipeline needs.** It accepts a PDF directly and returns schema-validated output. Both were exercised in the spike.
- **It is the fastest tier.** Extraction took 1.5 to 5.3 seconds per document in the spike.
- **It is the cheapest tier**, at $1 per million input tokens and $5 per million output tokens, half the price of Sonnet 5.5 and a quarter of Opus 5.5.

## What this decision is not based on

Cost at today's volume. At 50 billing documents a month the whole pipeline costs about $0.50 on Haiku and about $1.75 on Opus 5.5, so price alone would not justify the smaller model. The case for Haiku is that it is sufficient, fast, and leaves room to grow. See `docs/cost-and-latency.md`.

## Considered Options

- **Opus 5.5 or Sonnet 5.5 for everything**: rejected as the default. Affordable today, but it pays a premium on every document to help the few that need it. Escalation gives those few the stronger model.
- **Rules and per-vendor templates only**: rejected as the only method. Exact and free, but each vendor needs its own template and any layout change breaks it. Rules remain the fallback when the model fails, and a first tier for known vendors at scale.
- **A purpose-built invoice parsing service**: not evaluated. It would add a second vendor holding finance documents, and is worth revisiting if the eval shows a general model is not accurate enough.
- **Haiku 4.5 with escalation**: chosen.

## Consequences

The model's own confidence rating is not trusted alone. In the spike it reported high confidence on every document, which says nothing about how it behaves when wrong. Escalation and review are triggered by cross-checks and history checks as well (ADR 0004).

The model name is configuration, not code, so the default can change without a release.

Three clean, single-page documents are evidence that the approach works, not evidence of accuracy. That comes from the eval.
