# Design decisions

The outcome of the design interview, in one place. Decisions with a real trade-off behind them also have an ADR in `docs/adr/`. Terms are defined in `CONTEXT.md`.

## Architecture

| Area | Decision | ADR |
|---|---|---|
| Approach | Code, not a no-code workflow tool | 0001 |
| Mailbox access | Google's own APIs with our own OAuth app; Gmail scope is read-only | 0002 |
| Language and orchestration | Python with Prefect; core logic has no Prefect imports | 0003 |
| Pipeline | Discover, classify, extract, render, store, report, reconcile | |
| Extraction | Claude Haiku 4.5 with schema-validated output; Claude Sonnet 5.5 on low confidence; rules as fallback | 0008 |
| Classification | Built on both Claude Haiku 4.5 and Jev; the offline eval decides the default | 0009 |
| Ledger | SQLite, behind an interface so Postgres can replace it | |
| Dashboard | FastAPI and React; the only place people take actions | 0006 |
| Dashboard access | Google sign-in, restricted to an allowlist | |

## Collection rules

| Area | Decision |
|---|---|
| What is collected | Invoices, receipts and credit notes; one summary row per charge |
| Invoice and receipt for one charge | One row; the invoice is the saved PDF |
| Billing signals | Payment-failed notices and renewal reminders are not collected, but are used to explain gaps |
| Collection month | Decided by invoice date; discovery searches a few days either side by arrival date |
| Duplicates | One PDF and one row, listing every source account it was found in |
| Filename | `YYYY-MM_Vendor_Amount-CUR.pdf`, total including tax, e.g. `2026-08_Slack_1250.00-USD.pdf`; a clash gets a `_2` suffix |
| Currency | Original amount and currency kept; INR equivalent at the invoice date's rate, stored with the row |

The filename adds the currency to the format in the assignment. This is deliberate: amounts in mixed currencies are ambiguous without it.

## Portal links

| Case | Handling |
|---|---|
| Tokenised link | Followed automatically; PDF saved |
| Login-gated link | Flagged as manual download needed; a person uploads the PDF in the dashboard and the tool files it |
| Vendor billing APIs | Designed for; one example connector built |
| Stored portal passwords | Not used |

## Outputs

| Output | Decision |
|---|---|
| Archive | Google Drive, one folder per collection month under a root folder, mirrored to a local folder |
| Summary | Google Sheet, mirrored to CSV; read-only; confirmed billing documents only |
| Pending review | A read-only tab in the sheet, each row linking to that item in the dashboard; PDFs sit in a pending subfolder |
| Owner | One of the three test accounts owns the archive and summary |

## Expected vendors and gaps

| Area | Decision |
|---|---|
| Expected vendor list | Seeded from a file in the repo, then held in the ledger and edited in the dashboard |
| Each entry | Vendor, source account, billing cycle, renewal month for annual plans, usual amount |
| Suggested vendors | Proposed from past months; accepted or ignored in the dashboard |
| Gaps | Missing when every source account synced; unknown when one did not |

## Evals and failures

Everything in ADR 0004 and ADR 0005 is built, not only documented.

## Extras in scope

| Feature | Decision |
|---|---|
| Anomaly detection | Amount more than 30% from the vendor's usual, adjustable |
| Spend analytics | By vendor, by source account and by month, with month-on-month change |
| Digest | Slack only, by incoming webhook |
| Ask your invoices | Fixed set of queries chosen by the model (ADR 0007) |

Cost and run time are worked through in `docs/cost-and-latency.md`.
| Audit trail | Per billing document: source email, extraction result, and who corrected what and when |

## Seed data

| Area | Decision |
|---|---|
| Method | A script inserts messages through the Gmail API so senders look real; it uses separate write credentials |
| Volume | 15 emails per source account for August 2026, with lighter history for June and July |
| Hard cases | Non-invoices, duplicates across accounts, foreign currency, credit notes, multi-page PDFs, billing signals, a vendor absent on purpose |
| Portal pages | Two fake pages hosted for the seed data: one tokenised, one login-gated |

## Process

| Area | Decision |
|---|---|
| Workflow | Design decisions, prototypes, spec, tickets, test-first implementation, code review |
| Tracker | GitHub Issues |
| Repository | Public, named `adalatai-assignment` |
| Changes | One branch per ticket or group of related tickets, merged by pull request after automated review |
| Prototypes before the spec | Extraction spike on hand-made sample emails; review screen layout |

## Prototypes

| Prototype | Branch | Verdict |
|---|---|---|
| Extraction spike | `prototype/extraction-spike` | All three invoice formats routed, rendered and extracted correctly on Haiku 4.5 |
| Review screen | `prototype/review-screen` | Three panes chosen: review queue on the left, document in the middle, extracted fields on the right. Rejected: an editable table with bulk approval, and a one-at-a-time view |

## Parked until after the build

- Whether and where to deploy
- Offline mode
- An optional n8n layer on top
