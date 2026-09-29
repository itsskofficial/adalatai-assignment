---
status: superseded by 0017
---

# A workflow orchestrator (Prefect), not an agent framework (LangGraph)

The pipeline is deterministic: one routing decision on invoice format and one schema-validated LLM call for extraction. Nothing in it needs a model to decide what happens next, so an agent framework would add cost without benefit. What the pipeline does need is per-email retries, rate limiting against Gmail and the LLM, scheduling and run visibility, which is what a workflow orchestrator provides. We use Prefect, in Python.

## Considered Options

- **LangGraph**: rejected. It structures the reasoning inside one LLM-driven task; it does not distribute work, rate limit or schedule.
- **Dagster**: rejected. Monthly partitions fit well, but a variable number of emails per run fits its asset model poorly.
- **Temporal, Airflow**: rejected as too heavy to set up for a tool that must run from one command.
- **No framework**: rejected. We would end up rebuilding retries, rate limits and scheduling by hand.
- **Prefect**: chosen.

## Consequences

Core logic is written as plain functions with no Prefect imports; Prefect is a thin layer that calls them. Tests never touch the orchestrator, and replacing it is a contained change.

Prefect records runs. It does not replace the invoice ledger, which records which email produced which PDF.

The one open-ended step, retrieving an invoice from a billing portal, is the only place an agent loop is justified. It stays isolated behind the portal-link handler.
