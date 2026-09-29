---
status: accepted
---

# A run never takes away what an earlier run collected, and a document is known by its source

A month is run more than once: after a sign-in is renewed, after a failure, or on the schedule. Two rules make that safe.

**A run only adds.** If an email was collected before and this run cannot classify it, cannot read it, or judges it differently, the billing documents collected before stay in the summary and a warning says why. A model being down for an hour must not make an invoice vanish from finance's records.

**A billing document is identified by what it was made from**, not by the PDF that came out: the bytes of an attachment, the body of an email, or the address of a portal page. A run reads the email again to work out that identity. A document already known by it is then not rendered, not fetched from its portal and not read by a model again.

## Why the second rule

A PDF rendered by a browser carries its creation time, so rendering the same email body twice gives two files that differ byte for byte. Identifying documents by the PDF would have treated every re-run, and every copy of an email in a second source account, as a new document.

## Consequences

Re-running a month costs nothing for documents already collected, since no model is called for them.

A portal link carrying a single-use token is opened once. Later runs reuse what was collected.

A document held for review is not read again until a person has decided on it.

A decision made by a person is final for the run. An email a person marked as not a billing document is not held again by the next run.

Undoing a collection is a person's action in the dashboard, never a side effect of a run.
