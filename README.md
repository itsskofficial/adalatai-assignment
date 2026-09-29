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
     --expected-vendors samples/expected_vendors.json --allow-local-portals \
     --map engineering@nyayalabs.example=real1@gmail.com \
     --map ops@nyayalabs.example=real2@gmail.com \
     --map finance@nyayalabs.example=real3@gmail.com
   ```

   The sample expected vendor file names the sample accounts; `--map`, given as to the seed command, puts the real address in their place, so gaps name the mailbox the invoice should have reached. It also moves vendors already on the list from an earlier run.

   `--allow-local-portals` lets the collection follow portal links to this machine, which is where the sample portal pages are served. It exists only for those pages: never use it with real mail, where a link to this machine or the local network is refused on purpose. `--expected-vendors` fills the expected vendor list on the first run; with `--account` there is no default for it.

The output goes where the sample run's does: PDFs in `out/archive/2026-08/`, the summary in `out/2026-08_summary.csv`, the gaps in `out/2026-08_gaps.csv`, and every email examined in `out/ledger.sqlite`. Add `--google-owner ADDRESS` to also archive to Drive and write the Google Sheet, as above.

Each billing document's vendor is matched to the expected vendor list, so an invoice from "Amazon Web Services" is filed and reported as "AWS" when that is how the list names it; what the document said is kept in the ledger. Rules match first. For what they cannot decide, Jev is asked when `JEV_API_KEY` is set, otherwise Claude when `ANTHROPIC_API_KEY` is set; `--vendor-matcher rules` uses rules alone. A document collected under another name by an earlier run takes the list's name when its month is run again, and keeps its file. See [ADR 0016](docs/adr/0016-expected-vendor-spelling-wins.md).

The collection never opens a browser. An account that is not signed in, or whose sign-in has expired, is reported as not read (`Could not read ...`) and the other accounts are still collected; its gaps are unknown rather than missing. While the OAuth app is in testing, Google expires every sign-in after seven days: run step 1 again when that happens.

## Send the digest to Slack

Set `INVOICE_COLLECTOR_SLACK_WEBHOOK` to a Slack incoming webhook (`https://hooks.slack.com/...`) and each run posts a digest: billing documents collected, total spend, gaps and what needs review. A run that fails posts that instead. Set `INVOICE_COLLECTOR_DASHBOARD_URL` to link the digest to the dashboard's review screen. Without the webhook, or with `--no-digest`, nothing is sent. A digest that cannot be sent prints a warning and does not fail the run.

The webhook address is a secret: keep it in `.env`.

## Run on a schedule

`invoice-collector-flow` runs the same collection as a [Prefect](https://docs.prefect.io/) flow. It takes every option of `invoice-collector collect`, plus `--max-concurrent N`, the number of emails examined at once (default 5). That number also caps concurrent calls to the model.

Collect one month now:

```bash
cd backend
uv run invoice-collector-flow run 2026-08 --samples samples --out out
```

This needs no Prefect server. If `PREFECT_API_URL` points at a server that answers, the run is recorded there; otherwise Prefect starts a temporary one for the length of the run, which adds a few seconds.

Collect the previous month on the 3rd of each month at 06:00, India time:

```bash
cd backend
uv run prefect server start          # in another terminal; the interface is at http://127.0.0.1:4200
uv run prefect config set PREFECT_API_URL=http://127.0.0.1:4200/api
uv run invoice-collector-flow serve --samples samples --out out
```

`serve` keeps running and starts each collection when it is due. The month is worked out from the time the run was scheduled for, so a run that starts late still collects the right month. To collect straight away, run `uv run prefect deployment run 'invoice-collection/monthly'`.

The Prefect interface shows:

- each run, named `invoice-collection-2026-08`, with a `collect-month-2026-08` run inside it
- one task per email, named after its source account and subject, with its state, retries and logs
- an artifact, `collection-2026-08`, with the billing documents collected, the count of emails in each state, the gaps, the source accounts that could not be read, and the warnings

An email whose examination fails unexpectedly (a dropped connection, say) is tried again after about 10 and then 20 seconds, and is then recorded as failed with the reason. The other emails carry on. Failures the pipeline expects, such as a PDF nothing can read, are recorded straight away and not retried.

Prefect is optional. `invoice-collector collect` runs the same collection, one email at a time, without it.

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

The dashboard shows the summary of a collection month to people who sign in with Google and are allowed in.

It needs three things:

- `INVOICE_COLLECTOR_SESSION_SECRET`: a long random value that signs the session cookie. The dashboard will not start without it.
- `INVOICE_COLLECTOR_ALLOWLIST`: the administrators set by the installation, separated by commas. See [Who may sign in](#who-may-sign-in).
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

### Who may sign in

A person may sign in when their address is in the `INVOICE_COLLECTOR_ALLOWLIST` setting or on the list on the People screen. Addresses are matched whatever their capitals, and the check is made on every request, so a person removed from the list is refused on their next click without waiting for their session to end.

Each person has one of two roles:

- **Administrator**: may use the whole dashboard, including the People screen, where they add and remove people, change roles, and see recent refused sign-ins.
- **Member**: may use everything except the People screen, which they do not see.

The addresses in `INVOICE_COLLECTOR_ALLOWLIST` are administrators set by the installation. They are shown on the People screen marked as such and cannot be changed or removed there. That makes the setting the way back in: if the list is emptied or changed by mistake, an address in the setting can still sign in and put it right. Put at least one address in it. If the setting is empty and the list holds nobody, the dashboard refuses to start, since nobody could sign in.

The list, who added each person and when, their last sign-in, the history of changes, and refused sign-ins are kept in the ledger file. Sign-ins and refused sign-ins are recorded once a run has written the ledger.

### Review held billing documents

When a run doubts what it read from a billing document, the email needs review: the PDF waits in `out/archive/<month>/pending/` and the document stays out of the summary. The Review screen lists these emails for the chosen month, shows each PDF beside the fields read from it with the doubted fields marked and the reasons given, and offers two decisions:

- **Approve**, after correcting any field. The PDF moves to `out/archive/<month>/` under the name the confirmed fields give it, its rupee amount is looked up for the confirmed currency and date, and it appears in the summary. An invoice date in another month is refused, since the document belongs to that month's collection.
- **Not a billing document**. The email is recorded as skipped and the pending PDF is deleted.

A later run of the month keeps both decisions: an approved document is known by its content and is not read again, and an email judged not to be a billing document is not examined again. Every decision is recorded with who made it, when, and each field before and after. The screen opens at one email with `/review?month=2026-08&email=<message id>`, the form the Google Sheet and the Slack digest link with. Emails whose portal link needs a sign-in are listed apart with the link; the PDF downloaded from it is handed to the tool on a screen of its own.

Approving with `--ledger out/ledger.sqlite` files into `out/archive/`, the folder the collection wrote. When the collection archives to Google Drive, start the dashboard with the same owner account, and an approved PDF is filed to Drive first, as the run files one, and the summary links to it there:

```bash
uv run invoice-collector-dashboard --ledger out/ledger.sqlite --google-owner ADDRESS
```

The dashboard refuses to start if the owner account is not signed in to Drive. If Drive cannot be reached when a document is approved, nothing is changed and the email stays held, so it can be approved again. Without `--google-owner`, an approved PDF is filed locally only; the next collection does not copy it to Drive either, since it reads no document twice. The copy the run put in the Drive pending folder is left there.

### Corrections feed the golden dataset

When a confirmed field differs from what was read, the approval appends one line to `out/corrections.jsonl`, beside the ledger. Each line holds the document's content hash, the fields as read (`extracted`), the fields as confirmed (`confirmed`), which fields changed, the doubts the run raised, whether a stronger model read it again, and who confirmed it and when. An approval that changes nothing adds no line. The file is never written into `backend/samples/`.

To add corrections to the golden dataset (see [ADR 0004](docs/adr/0004-evals-from-labelled-seed-data.md)), periodically and by hand:

1. Read each new line and decide whether it is a reading failure worth keeping. Skip corrections that only reflect a business choice, such as a vendor renamed to match the vendor list.
2. Copy the source email (from the source account named in the line) and its PDF (from `out/archive/<month>/`) into the samples, with personal data removed, as a hard case.
3. Add an entry to `golden.json` with the `confirmed` fields as the correct answer, and label the hard case by its `doubts`. For a PDF attachment the content hash is the hash in `answers.json`, so its entry there takes the `confirmed` fields too.
4. Run the offline eval (`uv run invoice-collector-eval`) and commit the new cases with the scorecard, so every later change to a prompt or model is scored on the failure a person found.

## Connect source accounts in the dashboard

The Source accounts screen connects, renews and removes source accounts without a command line, and chooses the owner account. Connecting sends you to Google to sign in as the address being connected and allow read-only access to its mail; the owner account is also asked for access to the Drive files the tool creates. If a different address signs in, the connection is refused and nothing is stored. You then come back to the screen, which says whether it worked.

Before the first connection, add the redirect URI `http://localhost:8000/accounts/callback` to the same OAuth client for a web application in the Google Cloud console (APIs & Services, Credentials, the web client, Authorized redirect URIs), next to `http://localhost:8000/auth/callback`.

Sign-ins are stored in `credentials/tokens/`, the folder `invoice-collector-setup` uses, so an account connected in the dashboard is connected for the command line too, and the reverse. Accounts signed in from the command line but not connected are listed as found on this machine, with a button to add them. The dashboard never sends a stored sign-in to the browser. Removing a source account deletes its stored sign-in; what was collected from it stays in the ledger. Each connection, renewal, removal and change of owner is recorded with who made it and when.

While the OAuth app is in testing, Google ends each sign-in seven days after it is made. The screen shows when each sign-in made in the dashboard ends, marks one that ends within two days, and renews it with one button. For a published app, set `INVOICE_COLLECTOR_SIGN_IN_LIFETIME_DAYS=off`. Set `INVOICE_COLLECTOR_TOKEN_DIR` to keep sign-ins elsewhere.

To collect every connected source account, with no list to keep:

```bash
cd backend
uv run invoice-collector collect 2026-08 --out out --connected-accounts
```

The accounts are read from `out/ledger.sqlite`, so give the dashboard the same ledger (`--ledger out/ledger.sqlite`).

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
- `DECISIONS.md`: every decision in one place, each linked to its ADR
- `docs/research/cost-and-latency.md`: what it costs to run and how long a run takes
