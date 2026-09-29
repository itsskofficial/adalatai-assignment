---
status: accepted
---

# Failures are isolated per email, and nothing is dropped or guessed

The output is financial records, so a wrong value is worse than a missing one and a silently missing one is worst of all. Every email therefore ends in exactly one of four states: collected, needs review, skipped as a non-invoice, or failed with a reason. All four appear in the summary. A failure in one email or one account never stops the rest of the run.

| Failure | Response |
|---|---|
| Gmail rate limit or network error | Retry with backoff, per email |
| Account sign-in expired | Account marked failed; other accounts complete; summary names the missing account |
| LLM call fails or returns invalid output | Retry, then fall back to rules, then send to review |
| PDF corrupt or password-protected | Saved as-is and flagged for review |
| Portal link needs a login | Flagged as manual download needed, with the link |
| Crash mid-run | Re-run resumes from the ledger without creating duplicates |

## Consequences

Gap detection depends on sync status. If an account failed to sync, an expected vendor with no invoice is reported as **unknown**, not **missing**, because the invoice may be sitting in unread mail.

Every step must be idempotent. Invoices are keyed on Gmail message ID plus content hash.
