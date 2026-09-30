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
- `out/2026-08_skipped_and_failed.csv`: each source account that could not be read, and each email skipped or failed, with the reason (failed emails are also printed)
- `out/ledger.sqlite`: every email examined, its state, and which email each PDF came from; and every run, with how it was started (`command_line`, `schedule` or `dashboard`), when it started and finished, the outcome of each email it examined, and which source accounts it could not read and why. A run that crashed stays recorded as unfinished.

The filename adds the currency to the format in the brief (`YYYY-MM_Vendor_Amount.pdf`). Invoices arrive in several currencies, and an amount with no currency is ambiguous.

### Also archive to Google Drive and write a Google Sheet

Sign the owner account in once with `uv run invoice-collector-setup --owner ADDRESS`, then add `--google-owner ADDRESS` to the collect command. The PDFs then also go to the owner's Drive, in `Invoice Collection/2026-08/`, and the summary to a sheet named `Invoice summary 2026-08` in `Invoice Collection/`, beside the folders of the months (the folder is named on the [Settings screen](#choose-the-schedule-and-the-drive-folder)), with tabs for the summary, emails pending review (linking into the dashboard), skipped and failed emails with the source accounts that could not be read, and billing signals. The summary links each row to its PDF in Drive. The local folder and CSV are still written, also when Drive cannot be reached: the email is then recorded as failed with the reason, its PDF is kept locally, and the next run files it in Drive. Re-running a month updates the sheet and folder in place.

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

A gap says why nothing was collected when the tool knows: a billing document of the vendor held for review, an invoice behind a portal that needs a sign-in (download it and upload it on the Review screen), an email from the vendor that failed and why, or a payment that failed. The explanation is the same in the printed gaps, the gaps CSV, the digest and the Summary screen.

## Send the digest to Slack

Set `INVOICE_COLLECTOR_SLACK_WEBHOOK` to a Slack incoming webhook (`https://hooks.slack.com/...`) and each run posts a digest: billing documents collected, total spend, gaps, what needs review and what the run's model calls cost. A run that fails posts that instead. Set `INVOICE_COLLECTOR_DASHBOARD_URL` to link the digest to the dashboard's review screen. Without the webhook, or with `--no-digest`, nothing is sent. A digest that cannot be sent prints a warning and does not fail the run.

The webhook address is a secret: keep it in `.env`.

## Read several emails at once

By default the collect command examines one email after another. `--max-concurrent N` fetches and reads up to N emails at once, each on a thread of its own:

```bash
cd backend
uv run invoice-collector collect 2026-08 --samples samples --out out --max-concurrent 5
```

Asking a model takes most of a run's time, so model calls and Gmail reads go on at once; N is also the ceiling on model calls in flight, which keeps a run inside the rate limits of Gmail and the model provider. Rendering an email body and fetching a portal page share one browser on a thread of its own, so they still happen one at a time. Weighing each document against the ledger, filing it and recording the email happen one email at a time too, so the checks (a second invoice from one vendor this month, say) give what a run one email at a time gives. The same email in several source accounts is examined one copy after the other, so the second finds the document the first collected and does not read it again. The option works in `--run-options` of the dashboard too.

Whatever N is, an email whose examination fails unexpectedly (a dropped connection, say) is tried again after 10 and then 20 seconds, and is then recorded as failed with the reason. The other emails carry on. Failures the pipeline expects, such as a PDF nothing can read, are recorded straight away and not retried.

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

## Run the offline eval

The eval scores classification, extraction and vendor matching on two golden sets, and "Ask your invoices" on a set of questions. Every answer is compared with the one right answer; no model judges anything.

- **Standard set**: the sample mail in `backend/samples`.
- **Hard set**: `backend/evals/hard`, 28 hand-written emails of the kinds real invoices get wrong: legal entity names, several dates on one document, amounts due of zero, decimal commas and lakh grouping, a `$` that is not the US dollar, two PDFs in one email, forwarded invoices, quotes and statements, German, French and Hindi, and text telling the reader what to answer. Each answer carries a note saying why it is right. Regenerate it with `uv run invoice-collector-seed hard --out evals/hard`, which never touches `samples`.
- **Questions**: `backend/evals/questions.json`, 57 questions in English, Hinglish and Hindi, each with the fixed query and parameters that answer it, or a decline. Today is fixed at 2026-09-29, so "last month" has one right answer.

```bash
cd backend
uv run invoice-collector-eval run --estimate-only   # what the calls not yet cached would cost
uv run invoice-collector-eval run                   # all four evals, both golden sets
uv run invoice-collector-eval run --set hard --eval classification --eval extraction --eval matching
uv run invoice-collector-eval run --eval questions --asker claude-haiku --asker claude-sonnet
uv run invoice-collector-eval check                 # fail when a score fell below the baseline
uv run invoice-collector-eval render                # write the scorecard again from its stored results
```

Keys are read from `ANTHROPIC_API_KEY` and `JEV_API_KEY`, or from `.env`; a candidate without its key is reported as not run. A vendor matcher is scored as a run uses it: the rules decide what they can, and the model is asked only the rest. Each run writes `backend/evals/scorecard.md` and `scorecard.json`, replacing the last ones, with each set reported on its own. Scores of the hard set are named `hard.…` and those of the questions `questions.…`, so they are never compared with the standard set. `invoice-collector-eval accept` makes a scorecard's scores the new baseline.

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

To serve the pages from the dashboard service itself, as one container would, build the front end and give the service the folder it wrote. Pages and API then share http://localhost:8000, and sign-in comes back there:

```bash
cd frontend && npm run build
cd ../backend
INVOICE_COLLECTOR_FRONTEND_DIR=../frontend/dist uv run invoice-collector-dashboard --ledger out/ledger.sqlite
```

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

A later run of the month keeps both decisions: an approved document is known by its content and is not read again, and an email judged not to be a billing document is not examined again. Every decision is recorded with who made it, when, and each field before and after. The screen opens at one email with `/review?month=2026-08&email=<message id>`, the form the Google Sheet and the Slack digest link with. Emails whose portal link needs a sign-in are listed apart with the link, and take the PDF downloaded from it: see [Upload a PDF from a portal that needs a sign-in](#upload-a-pdf-from-a-portal-that-needs-a-sign-in).

Approving with `--ledger out/ledger.sqlite` files into `out/archive/`, the folder the collection wrote. When the collection archives to Google Drive, start the dashboard with the same owner account, and an approved PDF is filed to Drive first, as the run files one, and the summary links to it there:

```bash
uv run invoice-collector-dashboard --ledger out/ledger.sqlite --google-owner ADDRESS
```

The dashboard refuses to start if the owner account is not signed in to Drive. If Drive cannot be reached when a document is approved, nothing is changed and the email stays held, so it can be approved again. Once a document is approved or judged not a billing document, its copy in the pending folder is removed, locally and from Drive, where it is moved to the bin. A copy that cannot be removed does not undo the decision: the Review screen says which copy was left, to remove by hand. Without `--google-owner`, an approved PDF is filed locally only; the next collection does not copy it to Drive either, since it reads no document twice, and the copy the run put in the Drive pending folder is left there, which the Review screen also says.

### Upload a PDF from a portal that needs a sign-in

The tool never signs in to a vendor's portal. When the link in a billing email leads to a sign-in page, the run flags the email as needing a manual download, and the summary counts it as needing review. On the Review screen these emails are listed under **Manual download needed**, each with its sender, subject, the date it arrived, the source account it arrived in, and the portal link.

1. Open the portal link, sign in, and download the invoice or receipt as a PDF.
2. Choose that PDF beside the email and select **Upload**.

The tool reads the PDF with the same models and checks a run uses, then says what became of it:

- **Filed**: nothing was doubted. The PDF is filed under the name its fields give it, in the folder of the month of its invoice date (and to the owner account's Drive when the dashboard was started with `--google-owner`), and it appears in the summary.
- **Held for review**: a check raised a doubt, such as an unsure reading or a total far from the vendor's usual. It is opened in the review queue with the reasons, and approved or judged not a billing document like any other held document.
- **Already collected**: the same PDF was collected before, for example as an attachment in another source account. The email is linked to that document and nothing is filed twice.

One upload settles every email carrying the same portal link, in any source account. Uploading the same file again changes nothing. A later run of the month knows the document by its portal link, so it does not open the link again and does not take the upload away. If the same PDF later arrives attached to an email, the run links that email to the uploaded document instead of filing a second charge. A held upload dated in another month stays in that month's review queue when the month the email arrived in is run again.

The vendor an upload names is matched to the expected vendor list as a run matches it: "Zoom Video Communications" is filed and summarised as "Zoom" when the list says Zoom, and the name as read is kept in the ledger. The dashboard matches with the same models a collection uses: rules, then Jev when `JEV_API_KEY` is set, then Claude when `ANTHROPIC_API_KEY` is.

The file must be a PDF by its content, whatever it is called, without a password, and at most 20 MB. It is never opened or rendered by the tool; only its text is read. A file that is not a PDF, or that the reader finds is not a billing document or cannot read, is refused with the reason, and the email stays flagged so another file can be uploaded. Without `ANTHROPIC_API_KEY`, rules read an upload, and what rules read is always held for review.

Each upload is recorded with who made it, when, the file's size and hash, and what became of it. The uploads of a month are listed at `GET /api/months/<month>/review/uploads`, and each upload is a step in its document's history, followed by how it was read, matched, checked and filed.

### Corrections feed the golden dataset

When a confirmed field differs from what was read, the approval appends one line to `out/corrections.jsonl`, beside the ledger. Each line holds the document's content hash, the fields as read (`extracted`), the fields as confirmed (`confirmed`), which fields changed, the doubts the run raised, whether a stronger model read it again, and who confirmed it and when. An approval that changes nothing adds no line. The file is never written into `backend/samples/`.

To add corrections to the golden dataset (see [ADR 0004](docs/adr/0004-evals-from-labelled-seed-data.md)), periodically and by hand:

1. Read each new line and decide whether it is a reading failure worth keeping. Skip corrections that only reflect a business choice, such as a vendor renamed to match the vendor list.
2. Copy the source email (from the source account named in the line) and its PDF (from `out/archive/<month>/`) into the samples, with personal data removed, as a hard case.
3. Add an entry to `golden.json` with the `confirmed` fields as the correct answer, and label the hard case by its `doubts`. For a PDF attachment the content hash is the hash in `answers.json`, so its entry there takes the `confirmed` fields too.
4. Run the offline eval (`uv run invoice-collector-eval`) and commit the new cases with the scorecard, so every later change to a prompt or model is scored on the failure a person found.

### The history of a billing document

Each row of the summary has a **History** link, and the Review screen links to the history of the document it shows. The history is a timeline, oldest step first, for that one billing document:

- the email arriving, with its sender, subject and date, once for each source account it arrived in;
- how the email was classified, by which classifier, as what and with what confidence;
- how the document was found: as a PDF attachment (with its file name), in the email body, or behind a portal link (with the portal's host only);
- for a portal link that needs a sign-in, the PDF a person uploaded, with who uploaded it, when, the file's size and what became of it;
- each attempt at the email that raised something unanticipated, such as a dropped connection, and was tried again;
- the model that read it, the fields it read and its own confidence, or, for a damaged or password-protected PDF, that it could not be opened and was saved as it is;
- the expected vendor it was matched to, when it named the vendor another way, with the name as read and what matched it: rules, or the model (Jev or Claude) by name;
- each check that ran, passed or doubted, with every doubt;
- the stronger model reading it again, if the first reading was doubted, and each field it read differently;
- whether it was held for review or collected, where it was filed, and the rate to rupees with that rate's date;
- each correction a person made, with who, when, and the value before and after, and who approved or rejected it and when;
- the copy in the pending folder being removed after the decision, or left in place with the reason.

A run records each step in the ledger's `document_events` table as it happens; the dashboard records filing on approval, the removal of the pending copy, and each upload with the steps that followed it. Opening an earlier ledger adds the table. A document collected before the table existed has a shorter history: the emails, where it was filed and its rate, without times, and a note saying so. The history shows facts about an email and never its body, and all its text is shown as text. It opens at `/documents/<content hash>?month=2026-08`; the API gives it at `GET /api/billing-documents/<content hash>/trail`.

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

## See runs and run a month again in the dashboard

The Runs screen lists every run of the chosen collection month, newest first, whether it was started by the schedule, from the command line, or from the dashboard, and then by whom. Each run shows:

- whether it is running, finished, stopped or not finished;
- the emails it found, and how many were collected, need review, were skipped and failed;
- how long it took, and what its model calls cost, in all and for each model with its calls and tokens. A run that called no model shows $0; a run from before costs were metered reads "Not recorded", never zero; a cost that could not be worked out, because a model has no known price, reads "Unknown";
- each source account it could not read, and why.

Any signed-in person, member or administrator, can start a run:

- **Run the month again** reads every source account connected on the Source accounts screen, as `--connected-accounts` does.
- **Run an account again**, beside a source account that could not be read, reads that account alone. It is offered while the account's latest read for the month still failed and it is still connected. The other accounts' documents, gaps and counts are left as they are, and a run never takes away what was collected (ADR 0013).

A run started here is the collect command's own run: it files to the archive, rewrites the summary and the Google Sheet, and sends the Slack digest, just as a run from the command line or the schedule does. The page answers at once, the run goes on in the background, in the runner service when the dashboard has one and in the dashboard service when it does not (see [Run the runner service](#run-the-runner-service)), and the screen follows it until it ends. One run of a month goes on at a time; asking for a second is refused with the reason. A run that could not start is listed with why.

A run that did not finish, started a way the service performing runs handles, is shown as stopped: that service stopped while it ran, or the run failed, with the reason when there is one. The runner handles runs from the dashboard and the schedule; the dashboard, without a runner, those from the dashboard. Any other run that has not finished, such as one from the command line, is shown as not finished, since the dashboard cannot tell whether it is still going on elsewhere. When the runner cannot be reached the screen says so, no run can be started, and its unfinished runs are shown as not finished.

The run uses the dashboard's ledger folder as its output, its token folder, and its owner account (`--google-owner`), so start the dashboard with the ledger the collection writes, `out/ledger.sqlite`. With a ledger named otherwise the dashboard warns and starts no runs. Other options of the collect command are given in one quoted text:

```bash
uv run invoice-collector-dashboard --ledger out/ledger.sqlite --google-owner ADDRESS \
  --run-options="--no-exchange-rates"
```

The dashboard sets the source accounts, the output folder, the token folder and the owner account itself, and refuses to start if `--run-options` names any of them. Model keys and the Slack webhook are read from the environment, as for the command line.

## Choose the schedule and the Drive folder

The Settings screen holds the choices about collecting that finance may want to change without an engineer. Everyone signed in sees them; administrators change them.

- **Schedule**: on or off, the day of the month (1 to 28, so it falls in every month), the time of day, and the time zone (Asia/Kolkata unless chosen). The screen shows when the next run is due and which collection month it will collect: on the chosen day of each month the runner collects the month that has just ended. Saving a change to the schedule tells the runner, which works out its next run again. If the runner cannot be reached, the change is still saved and the screen says the runner will pick it up when it starts. Without a runner service, the screen says nothing runs on the schedule.
- **Drive folder**: the name of the folder at the top of the owner account's Drive where PDFs and summary sheets go, `Invoice Collection` unless chosen. Changing it affects later runs, and documents approved or uploaded on the Review screen after the change; nothing already filed is moved.

Settings are kept in the ledger, with who changed each one, when, and its value before and after, listed at the foot of the screen. Settings never set have their defaults, and the schedule starts off. API keys, the OAuth client, the Slack webhook and the runner's shared secret are not settings of this screen: they stay in the environment, set by whoever deploys the tool. The API is `GET` and `PUT /api/settings`.

## Run the runner service

The tool runs as three roles on one machine, sharing one disk with the ledger, the PDFs and the stored sign-ins:

| Role | What it does |
|---|---|
| Front end | The screens, in the browser: the files `npm run build` writes, served by the app |
| App | `invoice-collector-dashboard`: the API and the built front end. It never collects |
| Runner | `invoice-collector-runner`: performs every run, from the dashboard and on the schedule. It serves no pages |

The app asks the runner directly, over the private network between them, to start a run; the runner answers at once and performs the run in the background, one run of a month at a time. Nothing polls. The same arrangement runs on a developer's or reviewer's machine:

```bash
cd backend
# in one terminal
INVOICE_COLLECTOR_RUNNER_SECRET=<a long random value>   uv run invoice-collector-runner --ledger out/ledger.sqlite --google-owner ADDRESS
# in another
INVOICE_COLLECTOR_RUNNER_URL=http://127.0.0.1:8001 INVOICE_COLLECTOR_RUNNER_SECRET=<the same value>   INVOICE_COLLECTOR_FRONTEND_DIR=../frontend/dist uv run invoice-collector-dashboard --ledger out/ledger.sqlite
```

The runner takes the ledger the dashboard uses, which must be named `ledger.sqlite` since a run writes beside it, the owner account (`--google-owner`), and further collect options for every run in `--run-options`, such as `--run-options="--max-concurrent 5"`. It sets the source accounts, the output folder, the token folder and the owner account itself. Model keys and the Slack webhook are read from the environment, as for the collect command. Each run is the collect command's own run, recorded as started from the dashboard, with who asked, or by the schedule.

| Setting | Where | Default | What it is |
|---|---|---|---|
| `INVOICE_COLLECTOR_RUNNER_SECRET` | both | none: required | Sent by the app with every request and compared in constant time, so reaching the runner's port is not enough to start a run. The runner will not start without it |
| `INVOICE_COLLECTOR_RUNNER_HOST` | runner | `127.0.0.1` | The address it listens on. In containers, the private network only; never publish its port |
| `INVOICE_COLLECTOR_RUNNER_PORT` | runner | `8001` | The port it listens on |
| `INVOICE_COLLECTOR_RUNNER_URL` | app | not set | The runner's address, such as `http://runner:8001`. When set, runs are asked of the runner and `--run-options` belong to it; when not, the dashboard performs runs on its own threads |
| `INVOICE_COLLECTOR_TOKEN_DIR` | runner | `credentials/tokens` | Where stored sign-ins are kept, as for the dashboard |

**Which performs the runs.** With `INVOICE_COLLECTOR_RUNNER_URL` set, the runner does, and the dashboard never collects: this is how the tool is deployed. Without it, the dashboard performs runs itself on threads of its own, so a developer can run the dashboard alone; there is then no schedule.

**The schedule.** The runner works out from the ledger when the next scheduled run is due, sleeps until then, and runs. On the chosen day of month M it collects month M-1, the month that has just ended, worked out in the schedule's time zone (Asia/Kolkata unless chosen). The schedule is off until it is turned on, on the [Settings screen](#choose-the-schedule-and-the-drive-folder). When the schedule changes, the app tells the runner, which works the moment out again; a runner that was not told checks the settings again when its timer fires, and does not run at a moment they no longer give. When the runner starts, it looks once for a scheduled run that was due while it was not running, with no run of that month finished since, and performs it.

**Health.** `GET /health` on the runner needs no secret and says whether it is up, whether a run is going on (and of which month) and when the next scheduled run is due, for whatever watches the container.

Both processes write the same SQLite file. Every connection waits up to thirty seconds for another writer, and the file keeps write-ahead logging, so a reader never waits for a writer. This needs both processes on one machine with one disk.

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

### Browser tests

`backend/tests/browser` drives the dashboard in headless Chromium: the built front end against the real API, doing what finance does on the screens. The ledger behind them is written by the collect command's run over the sample mail, and only the outside world is faked, so no test reaches Google, Claude, Jev or Slack. They need the front end built, and are skipped with a message saying so when it is not:

```bash
cd frontend && npm ci && npm run build
cd ../backend
uv run playwright install chromium   # once
uv run pytest -m dashboard           # the browser tests alone
```

`uv run pytest` runs them with everything else. Build the front end again after changing it, since the tests use what is in `frontend/dist`. CI builds it and runs them in a job of their own.

## Read more

- `CONTEXT.md`: the terms used throughout
- `docs/adr/`: decisions and the reasons behind them
- `DECISIONS.md`: every decision in one place, each linked to its ADR
- `docs/research/cost-and-latency.md`: what it costs to run and how long a run takes
