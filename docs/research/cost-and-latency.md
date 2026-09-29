# Cost and latency

What the tool costs to run and how long a run takes, at today's volume and as it grows.

Every figure is marked as **measured** (from the extraction spike, 2026-09-29), **priced** (from Anthropic's published rates), or **assumed** (an estimate to be replaced with real numbers once the pipeline runs).

The measured cost per billing document of a real run replaces the assumed one. It has a place below, in [Measured cost per billing document](#measured-cost-per-billing-document), and a command that produces it from the run's traces in Langfuse. **It has not been measured yet**: no live run has been traced, so the section is empty, and every cost in this document is still the estimate worked out from the assumptions.

## Summary

| | Today | Whole company | Many organisations |
|---|---|---|---|
| Source accounts | 3 | 30 | 3,000 |
| Billing documents per month | 50 | 500 | 50,000 |
| Candidate emails per day | 5 | 50 | 5,000 |
| Model cost per month | $0.95 | $7.20 | $250 to $500 |
| Run time | 2 to 3 minutes | 10 to 15 minutes | Continuous |

Model cost is about one US cent per billing document collected. This is an estimate from the assumptions below; the measured figure goes in [Measured cost per billing document](#measured-cost-per-billing-document) after a live run.

## Measured cost per billing document

**Not yet measured.** This section is filled from a live run, and nothing here is invented.

Every model call a run makes is traced in Langfuse with its tokens, its cost and the time it took, with the run as the trace's session (see [ADR 0017](../adr/0017-trace-metadata-not-finance-data.md)). The cost of each call is priced from the one table of prices in `backend/src/invoice_collector/metering.py`, which the run's own record of its cost and the eval use too: Claude's prices are those in the table below, and Jev's come from `docs/research/jev-api.md`. `invoice-collector cost` reads a run's calls back from Langfuse and prints this table for it: the calls, tokens, cost and median time of each step, the total, and the cost per billing document the run's month holds, collected or held. Below the table it sets each model's calls, tokens and cost as the traces have them beside what the run recorded in the ledger. Each call gives the tracer and the run's meter the same count of tokens, so the two agree unless traces were lost.

To measure it, with `ANTHROPIC_API_KEY` and Langfuse's keys set (see "Trace model calls in Langfuse" in the README), from `backend/`:

```bash
# Into an empty output folder, so every document is read; a second run reads nothing again.
# The mailboxes are seeded as the README's "Collect from real mailboxes" describes.
uv run invoice-collector collect 2026-08 --out out/measured \
  --account real1@gmail.com --account real2@gmail.com --account real3@gmail.com \
  --expected-vendors samples/expected_vendors.json --allow-local-portals \
  --map engineering@nyayalabs.example=real1@gmail.com \
  --map ops@nyayalabs.example=real2@gmail.com \
  --map finance@nyayalabs.example=real3@gmail.com
# A minute later, once Langfuse has taken in the traces:
uv run invoice-collector cost 2026-08 --out out/measured
```

Paste its output here, with the date of the run and the source accounts it read:

| Step | Model | Calls | Input tokens | Output tokens | Cost | Median seconds |
|---|---|---|---|---|---|---|
| classification | to be measured | | | | | |
| extraction | to be measured | | | | | |
| escalated extraction | to be measured | | | | | |
| vendor matching | to be measured | | | | | |
| **Total** | | | | | | |

Measured cost per billing document: **to be measured**.

Once it is filled in, it replaces the assumed figures it covers in the tables below: candidate emails per billing document (calls to classification per document), pages per document (input tokens per extraction), the share escalated to Sonnet 5.5, classification tokens and time, and the cost per billing document.

## Prices

Priced, per million tokens.

| Model | Input | Output |
|---|---|---|
| Claude Haiku 4.5 | $1.00 | $5.00 |
| Claude Sonnet 5.5 | $2.00 | $10.00 |
| Claude Opus 5.5 | $4.00 | $20.00 |

The batch interface halves these prices in exchange for results within hours instead of seconds.

## What was measured

One extraction call per document, PDF sent directly to Haiku 4.5.

| Document | Pages | Input tokens | Output tokens | Seconds |
|---|---|---|---|---|
| Slack invoice | 1 | 2,348 | 57 | 5.27 |
| Notion receipt | 1 | 2,317 | 57 | 2.29 |
| Figma invoice | 1 | 2,294 | 58 | 1.50 |
| **Average** | 1 | **2,320** | **57** | **3.02** |

The first call includes connection setup, so the median of 2.29 seconds is the better guide.

Cost of one measured call:

    input   2,320 tokens x $1.00 / 1,000,000 = $0.00232
    output     57 tokens x $5.00 / 1,000,000 = $0.00029
                                       total = $0.0026

## Assumptions

| Assumption | Value | Reasoning |
|---|---|---|
| Billing documents per month | 50 | From the assignment |
| Candidate emails per billing document | 3 | A keyword search also returns billing signals, newsletters and duplicates |
| Documents extracted per billing document | 1.2 | Duplicates across source accounts, and invoice and receipt pairs |
| Average pages per document | 2 | Usage-based vendors such as AWS send multi-page invoices |
| Input tokens per extraction | 4,600 | 2 pages x 2,300 measured per page |
| Output tokens per extraction | 60 | Measured 57 to 58 |
| Input tokens per classification | 800 | Email subject, sender and body text, no PDF |
| Output tokens per classification | 30 | A label and a reason |
| Share escalated to Sonnet 5.5 | 10% | To be replaced by the rate seen in the eval |
| Questions asked per month | 100 | About 5 per working day |
| Tokens per question | 3,000 in, 300 out | Query definitions, the question, and the result |

## Cost per step

| Step | Model | Calculation | Cost per call |
|---|---|---|---|
| Classification | Haiku 4.5 | (800 x $1 + 30 x $5) / 1,000,000 | $0.00095 |
| Extraction | Haiku 4.5 | (4,600 x $1 + 60 x $5) / 1,000,000 | $0.00490 |
| Escalated extraction | Sonnet 5.5 | (4,600 x $2 + 60 x $10) / 1,000,000 | $0.00980 |
| Question | Haiku 4.5 | (3,000 x $1 + 300 x $5) / 1,000,000 | $0.00450 |

## Cost per billing document

| Step | Calls per billing document | Cost per call | Cost |
|---|---|---|---|
| Classification | 3 | $0.00095 | $0.00285 |
| Extraction | 1.2 | $0.00490 | $0.00588 |
| Escalated extraction | 0.12 | $0.00980 | $0.00118 |
| **Total** | | | **$0.00991** |

## Monthly cost today

| Step | Calls | Cost per call | Monthly cost |
|---|---|---|---|
| Classification | 150 | $0.00095 | $0.14 |
| Extraction | 60 | $0.00490 | $0.29 |
| Escalated extraction | 6 | $0.00980 | $0.06 |
| Questions | 100 | $0.00450 | $0.45 |
| **Total** | | | **$0.95** |

Everything else is free at this volume: the Gmail, Drive and Sheets interfaces have no per-call charge within their quotas, the ledger is a local file, and the exchange rate source is free.

## What development costs

The offline eval is the largest model cost while the tool is being built, because it re-runs the whole golden dataset.

| | Value |
|---|---|
| Documents in the golden dataset | about 100 (assumed) |
| Cost per eval run | 100 x ($0.00095 + $0.00490) = $0.59 |
| Runs per month during development | 30 (assumed) |
| Monthly cost | $17.55 |

Two measures keep this down. The eval runs only when a prompt, model or extraction rule changes. Results are cached by a hash of the document and the prompt, so an unchanged pair is never paid for twice.

## Does the model choice matter for cost?

The pipeline for one month, 396,000 input tokens and 8,100 output tokens, priced on each model with no escalation:

| Model | Input cost | Output cost | Monthly cost |
|---|---|---|---|
| Claude Haiku 4.5 | $0.40 | $0.04 | $0.44 |
| Claude Sonnet 5.5 | $0.79 | $0.08 | $0.87 |
| Claude Opus 5.5 | $1.58 | $0.16 | $1.75 |

At 50 billing documents a month the difference is about $1.30, which is not a reason to choose one model over another. At 50,000 a month it is about $1,300. The reasons for choosing Haiku are in ADR 0008.

The larger models also count more tokens for the same document, up to roughly a third more, so their true cost is somewhat higher than this table shows.

## Cost as volume grows

| | Today | Whole company | Many organisations |
|---|---|---|---|
| Billing documents per month | 50 | 500 | 50,000 |
| Pipeline, at $0.00991 each | $0.50 | $4.95 | $495.50 |
| Questions | $0.45 (100) | $2.25 (500) | not estimated |
| **Total** | **$0.95** | **$7.20** | **about $500** |
| With the batch interface | not needed | not needed | about $250 |

Three measures reduce cost at the largest volume:

1. **Batch interface.** A monthly collection does not need answers in seconds, so it can take the 50% discount.
2. **Templates for known vendors.** A vendor whose layout is stable can be read by rules, with the model used only when the rules fail.
3. **Content hashing.** A document already seen in another source account is not extracted again.

## Latency

| Step | Time per item | Source |
|---|---|---|
| Classification | about 1 second | Assumed |
| Extraction | 2.3 seconds median, 1.5 to 5.3 range | Measured |
| Rendering an email body or portal page to PDF | about 1 second | Assumed |
| Fetching an email from Gmail | under 1 second | Assumed |

Run time for one month today:

    classification   150 calls x 1.0 s = 150 s
    extraction        66 calls x 2.3 s = 152 s
    rendering         40 docs  x 1.0 s =  40 s
                       one at a time   = 342 s, about 6 minutes
                       five at a time  = about 70 s

Adding Gmail fetches and uploads to Drive, a run should take 2 to 3 minutes with five items processed at a time.

The limit on going faster is not the tool but the rate limits of Gmail and the model provider. The orchestrator enforces a ceiling on concurrent calls so that a run slows down instead of failing.

## What the tool saves

| | Value |
|---|---|
| Time to collect one invoice by hand | 3 minutes (assumed) |
| Time per month by hand | 50 x 3 = 150 minutes |
| Time per month with the tool | Reviewing flagged items only |
| Model cost per month | $0.95 |

## Figures to replace after the first real run

`invoice-collector cost` measures the first four and the cost per billing document; see [Measured cost per billing document](#measured-cost-per-billing-document). The last two are not model calls: the total run time is the run's start and finish in the ledger, and rendering is not traced.

- Candidate emails per billing document
- Average pages per document
- Share escalated to Sonnet 5.5
- Classification tokens and time
- Rendering time
- Total run time
