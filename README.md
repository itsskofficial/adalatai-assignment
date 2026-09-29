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

### Also archive to Google Drive and write a Google Sheet

Sign the owner account in once with `uv run invoice-collector-setup --owner ADDRESS`, then add `--google-owner ADDRESS` to the collect command. The PDFs then also go to the owner's Drive, in `Invoice Collection/2026-08/`, and the summary to a sheet named `Invoice summary 2026-08` in `Invoice Collection/`, beside the folders of the months, with tabs for the summary, emails pending review (linking into the dashboard), skipped and failed emails, and billing signals. The summary links each row to its PDF in Drive. The local folder and CSV are still written, also when Drive cannot be reached: the email is then recorded as failed with the reason, its PDF is kept locally, and the next run files it in Drive. Re-running a month updates the sheet and folder in place.

## Collect from real mailboxes

This reads three real Gmail mailboxes, one per source account, instead of the folder of sample emails. For a demonstration, the sample emails are first put into those mailboxes. It needs the OAuth desktop client in `credentials/desktop-client.json` (see [ADR 0002](docs/adr/0002-direct-google-apis-over-managed-connectors.md)) and `ANTHROPIC_API_KEY` in `.env`, since Claude classifies and reads mail that has no prepared answers. Below, `real1@gmail.com`, `real2@gmail.com` and `real3@gmail.com` stand for the three mailboxes. Run every command from `backend/`.

1. Sign each source account in for reading. A browser window opens for each; the access asked for is read-only Gmail and nothing more.

   ```bash
   uv run invoice-collector-setup real1@gmail.com real2@gmail.com real3@gmail.com
   uv run invoice-collector-setup real1@gmail.com real2@gmail.com real3@gmail.com --status
   ```

   `--status` reports whether each read-only sign-in is present, expired or missing, and never opens the browser.

2. Put the sample emails into the mailboxes. Each sample account is mapped to the real address that receives its emails, with the `To` header rewritten to that address. Each account is signed in a second time, through the browser, with access only to insert mail; that sign-in is stored apart (`<address>.seeding.json`), so the read-only sign-in the collection uses is never widened. Existing messages are looked up with the read-only sign-in, so running this twice inserts nothing twice. Add `--dry-run` to list what would be inserted without signing in, and `--portal-base-url` to point portal links somewhere other than `http://localhost:8765`.

   ```bash
   uv run invoice-collector-seed gmail --samples samples \
     --map engineering@nyayalabs.example=real1@gmail.com \
     --map ops@nyayalabs.example=real2@gmail.com \
     --map finance@nyayalabs.example=real3@gmail.com
   ```

3. In a second terminal, serve the sample portal pages, so the tokenised and login-gated portal links in those emails open. It serves `samples/portal/` on port 8765 until interrupted with Ctrl+C.

   ```bash
   uv run invoice-collector-seed portal --samples samples
   ```

4. Collect the month, naming each source account with `--account`:

   ```bash
   uv run invoice-collector collect 2026-08 --out out \
     --account real1@gmail.com --account real2@gmail.com --account real3@gmail.com \
     --expected-vendors samples/expected_vendors.json --allow-local-portals
   ```

   `--allow-local-portals` lets the collection follow portal links to this machine, which is where the sample portal pages are served. It exists only for those pages: never use it with real mail, where a link to this machine or the local network is refused on purpose. `--expected-vendors` fills the expected vendor list on the first run; with `--account` there is no default for it.

The output goes where the sample run's does: PDFs in `out/archive/2026-08/`, the summary in `out/2026-08_summary.csv`, the gaps in `out/2026-08_gaps.csv`, and every email examined in `out/ledger.sqlite`. Add `--google-owner ADDRESS` to also archive to Drive and write the Google Sheet, as above.

The collection never opens a browser. An account that is not signed in, or whose sign-in has expired, is reported as not read (`Could not read ...`) and the other accounts are still collected; its gaps are unknown rather than missing. While the OAuth app is in testing, Google expires every sign-in after seven days: run step 1 again when that happens.

## Send the digest to Slack

Set `INVOICE_COLLECTOR_SLACK_WEBHOOK` to a Slack incoming webhook (`https://hooks.slack.com/...`) and each run posts a digest: billing documents collected, total spend, gaps and what needs review. A run that fails posts that instead. Set `INVOICE_COLLECTOR_DASHBOARD_URL` to link the digest to the dashboard's review screen. Without the webhook, or with `--no-digest`, nothing is sent. A digest that cannot be sent prints a warning and does not fail the run.

The webhook address is a secret: keep it in `.env`.

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
- `portal/`: the pages behind the portal links; serve them with `uv run invoice-collector-seed portal --samples samples`

The emails and the golden dataset are the same on every run. The PDFs are not, because the browser stamps each with its creation time, so commit the whole folder together.

## Open the dashboard

The dashboard shows the summary of a collection month to people who sign in with Google and are on the allowlist.

It needs three things:

- `INVOICE_COLLECTOR_SESSION_SECRET`: a long random value that signs the session cookie. The dashboard will not start without it.
- `INVOICE_COLLECTOR_ALLOWLIST`: the addresses allowed to sign in, separated by commas.
- `credentials/web-client.json`: the OAuth client for a web application, from the Google Cloud console, with redirect URI `http://localhost:8000/auth/callback`. Set `INVOICE_COLLECTOR_WEB_CLIENT_FILE` to keep it elsewhere.

The two variables can be set in `.env`.

```bash
cd backend
uv run invoice-collector-dashboard --ledger out/ledger.sqlite
```

```bash
cd frontend
npm install
npm run dev
```

Then open http://localhost:5173.

## Develop

```bash
cd backend
uv run pytest
uv run ruff check .
uv run pyright
```

```bash
cd frontend
npm run lint
npm run test
npm run build
```

## Read more

- `CONTEXT.md`: the terms used throughout
- `docs/adr/`: decisions and the reasons behind them
- `docs/design-decisions.md`: every design decision in one place
- `docs/cost-and-latency.md`: what it costs to run and how long a run takes
