---
status: accepted
---

# A billing document carries the expected vendor's spelling, and what it said is kept

A live run read "Amazon Web Services" from an invoice while the expected vendor list said "AWS". The invoice was filed and summarised as "Amazon Web Services", AWS was reported as a gap, and "Amazon Web Services" was offered as a suggested vendor. One charge showed up as three problems.

The run now matches the vendor of every billing document to the expected vendor list after it is read. A name that differs from a listed one only in case, punctuation or a legal suffix is that vendor. Otherwise the vendor matcher of ADR 0009 is asked: rules first, and a model only when the rules cannot decide. When it matches, the document carries the list's spelling everywhere: the file name, the summary, gaps, the checks against the vendor's usual amount, and suggestions. The name the document gave is kept in the ledger beside it, for the audit trail. When nothing matches, the name as read stands, and the vendor is suggested as before.

The list's spelling wins because the list is what the company wrote down and checks against. A person looking for AWS finds AWS.

## Documents collected before vendors were matched

A run never reads a known document again (ADR 0013), so a document collected under "Amazon Web Services" would otherwise stay under that name for good. On the next run of its month, its name is matched again, from the name and the email's text, and the ledger records it under the list's spelling with the old name kept beside it. No duplicate is made: the document is still known by its source.

Its PDF keeps its file and its file name. A run does not move or rename a filed PDF, since the file may already be linked from the summary, the sheet or a message, and Drive offers no rename under the access the tool has that would keep those links meaningful. So an old document's file name may show the name as read while the summary shows the list's spelling. New documents agree in both.

A suggestion that no charge supports any more is withdrawn by the run. It was the run's own guess, from a name that has now been matched.

A name a person confirmed on the Review screen is not matched again, since a person's decision is final (ADR 0013). A name matched once is not matched again either: what the document said is already beside it.

## Considered Options

- **Keep the name as read and match only when reconciling**: rejected. The file name, the summary, suggestions and the usual-amount check would each need the same matching, and each would be a place to forget it.
- **Rename old PDFs on a re-run**: rejected. It changes a filed record behind every link to it.
- **Leave old documents as they were**: rejected. The gap and the suggestion would stay until a person noticed and corrected the ledger by hand, which the dashboard offers no way to do.
- **Match on every run, file under the list's spelling, keep the name as read, leave old files where they are**: chosen.

## Consequences

The vendor matcher is chosen as the classifier is: Jev for what rules cannot decide when its key is set, otherwise Claude Haiku, otherwise rules alone. A model that fails leaves the rules' answer, which is no match, and a warning says so. The email is still collected.

A document whose name the rules cannot place calls a model once per run while it stays unmatched, including on re-runs. Jev costs a fraction of a cent a call.

Billing signals are matched the same way, so "payment failed" from "Zoom Video Communications" explains the gap of "Zoom".
