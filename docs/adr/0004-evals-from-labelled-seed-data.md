---
status: accepted
---

# Extraction quality is measured by evals, offline and online

Extraction uses an LLM, so correctness cannot be assumed from passing unit tests. We generate the seed invoices ourselves and therefore know the true vendor, amount, currency and date for each one; that labelled set is the golden dataset for an offline eval that gates every change to a prompt, model or extraction rule. In production, where there is no answer key, confidence scores and cross-checks decide whether an extraction is written to the summary or sent to review.

## Offline

- Golden dataset: every seed email with its expected output, plus hard cases (non-invoices, duplicates, foreign currency, credit notes, multi-page PDFs).
- Scored per field: exact match on amount and currency, normalised match on vendor, date within the billing period.
- Invoice classification is scored by precision and recall separately, because a missed invoice and a false invoice cost finance differently.
- The eval fails the build if accuracy drops. The scorecard is committed alongside the sample output.

## Online

- Each extracted field carries a confidence score; low confidence goes to the review queue.
- Cross-checks: amount in the email body against amount in the PDF; line items against the total.
- History checks: amount far from the vendor's usual, unexpected currency, more than one invoice from a vendor in a month.
- Corrections made during review are added to the golden dataset, so the offline eval grows from real failures.

## Consequences

The review queue is worked in the dashboard, not the summary sheet. See ADR 0006.
