# Extraction spike: result

Throwaway prototype. Not production code.

## Question

Can one email of each invoice format be turned into a correctly named PDF with correctly extracted fields, using Claude Haiku 4.5?

## Verdict

Yes. 3 of 3 samples passed on 2026-09-29.

| Sample | Format | Extracted | Input tokens | Output tokens | Seconds |
|---|---|---|---|---|---|
| Slack | PDF attachment | Slack, 2026-08-03, 652.50 USD, invoice | 2,348 | 57 | 5.27 |
| Notion | Email body | Notion, 2026-08-14, 221.40 EUR, receipt | 2,317 | 57 | 2.29 |
| Figma | Portal link | Figma, 2026-08-21, 190.00 USD, invoice | 2,294 | 58 | 1.50 |

## What it settled

- Rule-based routing separates the three formats.
- A headless browser renders email bodies and tokenised portal pages to PDF.
- Sending the PDF itself to the model works for all three formats, so one extraction path serves them all.
- The model returned the brand name, not the legal entity, and the invoice date, not the billing period.
- Schema-validated output needed no retries.

## What it did not settle

- Accuracy on messy documents. These three samples are clean and single-page.
- Whether the model's own confidence rating can be trusted. It reported "high" on all three.
- Multi-page documents, scanned documents, and non-English documents.

## How to run

    uv run prototypes/extraction_spike/make_samples.py
    uv run prototypes/extraction_spike/spike.py
