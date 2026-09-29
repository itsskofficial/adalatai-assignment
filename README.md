# Invoice Collection

Collects the billing documents that SaaS vendors email to a company's mailboxes each month, files them as PDFs, and reports what was found and what is absent.

This README covers what works today. It grows as the tool does.

## Run a collection against the sample emails

Requires [uv](https://docs.astral.sh/uv/).

```bash
cd backend
uv run invoice-collector collect 2026-08 --samples samples --out out
```

This writes:

- `out/archive/2026-08/`: one PDF per billing document, named `YYYY-MM_Vendor_Amount-CUR.pdf`
- `out/2026-08_summary.csv`: one row per billing document
- `out/ledger.sqlite`: every email examined, its state, and which email each PDF came from

The filename adds the currency to the format in the brief (`YYYY-MM_Vendor_Amount.pdf`). Invoices arrive in several currencies, and an amount with no currency is ambiguous.

## Regenerate the sample emails

`backend/samples/` is generated, together with its correct answers, from the vendor catalogue in `backend/src/invoice_collector/seed/`.

```bash
cd backend
uv run playwright install chromium
uv run invoice-collector-seed generate --out samples
```

This writes:

- `<source account>/*.eml`: three months of email for each source account
- `golden.json`: the golden dataset, one entry per email, with its hard-case labels
- `expected_vendors.json`: the expected vendor list
- `answers.json`: the extraction of each PDF attachment, by content hash
- `portal/`: the pages behind the portal links; serve them with `python -m http.server 8765 --directory samples/portal`

The emails and the golden dataset are the same on every run. The PDFs are not, because the browser stamps each with its creation time, so commit the whole folder together.

## Develop

```bash
cd backend
uv run pytest
uv run ruff check .
uv run pyright
```

## Read more

- `CONTEXT.md`: the terms used throughout
- `docs/adr/`: decisions and the reasons behind them
- `docs/design-decisions.md`: every design decision in one place
- `docs/cost-and-latency.md`: what it costs to run and how long a run takes
