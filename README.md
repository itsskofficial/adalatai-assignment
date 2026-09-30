# Invoice Collection

Collects the billing documents that SaaS vendors email to a company's Gmail mailboxes each month, files each one as a PDF, writes a summary with one row per charge, and reports every expected vendor with no billing document that month. A dashboard is where people review what the tool doubted, upload what it could not fetch, and run a month again.

## What a run produces

[`docs/sample-output/`](docs/sample-output/) holds the output of four runs, June to September 2026, over three test Gmail mailboxes filled with the sample mail, with a Claude key set. August is the month the sample mail is written around; [`2026-08/`](docs/sample-output/2026-08/) is the one described here:

| File | What it holds |
|---|---|
| `2026-08_summary.csv` | 15 charges: vendor, invoice date, amount, currency, the source accounts it was found in, a link to its PDF, document type, the amount in rupees and the rate used |
| `2026-08_gaps.csv` | 3 expected vendors with nothing collected, each with a status (`held_for_review`, `manual_download`, `payment_failed`) and why: Datadog held for review (its total is 39% above its usual), Google Workspace behind a portal that needs a sign-in, Zoom's payment failed |
| `2026-08_skipped_and_failed.csv` | 7 emails that were not collected, each with its reason |
| `archive/` | The 17 PDFs filed, and in `archive/pending/` the one held for review |

[`2026-06/`](docs/sample-output/2026-06/) and [`2026-07/`](docs/sample-output/2026-07/) are the two months before it, quieter: 17 and 18 charges, one gap each (Google Workspace behind its portal) and nothing skipped. [`2026-09/`](docs/sample-output/2026-09/) is the run started from the dashboard on the hosted copy ([docs/deploy.md](docs/deploy.md)), with the same three gaps as August, 16 charges and 8 emails skipped.

The links in the summary open the PDFs in the owner account's Google Drive, shared for anyone with the link. The Google Sheet `Invoice summary 2026-08`, in the same Drive folder, is shared the same way, and its four tabs are formatted as a report, with named headers, filters, dates, grouped amounts and file names that open the PDFs. The same PDFs are in `archive/` beside the CSVs.

Files are named `YYYY-MM_Vendor_Amount-CUR.pdf`, such as `2026-08_Slack_652.50-USD.pdf`. This adds the currency to the format in the brief, because invoices arrive in several currencies and an amount without one is ambiguous. Two documents that would share a name are kept apart with `_2`.

## Try it with Docker

You need Docker with Compose, a Google account to sign in to the dashboard with, and one to three Gmail addresses you are willing to fill with sample mail. Collecting only ever reads a mailbox, with a read-only sign-in. Filling it with sample mail (step 6) is the one action that writes to it, done on purpose, under a separate sign-in asked for at that moment.

### 1. Clone

```bash
git clone https://github.com/itsskofficial/adalatai-assignment.git
cd adalatai-assignment
```

### 2. Create the Google Cloud project and OAuth client

The tool uses its own OAuth app ([ADR 0002](docs/adr/0002-direct-google-apis-over-managed-connectors.md)), so you make one. These are the steps in the Google Cloud console as it was at the time of writing. The author did them once for this project; they are written out here from the settings the code asks for, and the console's wording may differ.

1. Create a project: [console.cloud.google.com](https://console.cloud.google.com), the project picker, **New project**.
2. Enable three APIs in it (APIs & Services, Library): **Gmail API**, **Google Drive API**, **Google Sheets API**.
3. Configure the consent screen (Google Auth Platform, or APIs & Services, OAuth consent screen):
   - User type **External**, publishing status **Testing**.
   - An app name and your address as support and developer contact.
   - Scopes (Data access): `openid`, `.../auth/userinfo.email`, `.../auth/gmail.readonly`, `.../auth/gmail.insert` and `.../auth/drive.file`. The last two are asked for only when you fill a mailbox with sample mail and when you make an account the owner account.
   - Test users (Audience): your own address, and every test mailbox you will connect. While the app is in testing, nobody else can sign in.
4. Create the client (Credentials, **Create credentials**, **OAuth client ID**):
   - Application type **Web application**.
   - Authorised redirect URIs: `http://localhost:8000/auth/callback` and `http://localhost:8000/accounts/callback`. No JavaScript origins are needed.
   - Keep the client ID and client secret it shows.

Google will warn that the app is unverified when you sign in. That is expected while the app is in testing: choose **Continue** (or **Advanced**, then go to the app).

### 3. Fill in `.env`

```bash
cp .env.example .env
```

Every setting in [`.env.example`](.env.example) says what it is for, whether it is required, and its default. For this path, set these and leave the rest empty:

| Setting | Value |
|---|---|
| `INVOICE_COLLECTOR_SESSION_SECRET` | A long random value: `openssl rand -hex 32`, or `python -c "import secrets; print(secrets.token_hex(32))"` |
| `INVOICE_COLLECTOR_RUNNER_SECRET` | Another long random value, made the same way |
| `INVOICE_COLLECTOR_ALLOWLIST` | Your own address. You become an administrator who can always sign in |
| `INVOICE_COLLECTOR_WEB_CLIENT_ID` and `INVOICE_COLLECTOR_WEB_CLIENT_SECRET` | From step 2 |
| `INVOICE_COLLECTOR_SAMPLE_PORTAL_URL` | `http://portal:8765`, so a run may open the sample portal and nothing else on the local network |
| `ANTHROPIC_API_KEY` | Optional. See below |
| `JEV_API_KEY`, `INVOICE_COLLECTOR_SLACK_WEBHOOK` | Optional. Jev classifies emails first when set; the webhook receives a digest after each run |

**Without a Claude key**, strict rules read each billing document: a labelled total, a date, and a vendor named on the expected vendor list or by the sender. What rules read is never trusted. Every document is held for review with the reason "read by rules, not by a model", nothing reaches the summary until a person approves it, and the Questions screen is unavailable. Over the sample mail for August this holds 21 emails for review and collects none (measured with the command line, below). **With a key**, Claude Haiku reads each document, a doubted reading is read again by Claude Sonnet, and only what the checks still doubt is held, as in the sample output.

### 4. Start it

```bash
docker compose --profile portal up -d --wait
```

This builds one image and starts three services: `app` (the dashboard, on `http://localhost:8000` only), `runner` (which collects, reachable only by the app) and `portal` (the sample portal pages, reachable only on the Compose network). The `portal` profile is what starts the third. The first start builds the image, which holds a headless Chromium, so it takes a while.

If a required setting is missing, the service names every problem at once in its log and does not start: `docker compose logs app`.

### 5. Sign in and connect the mailboxes

1. Open `http://localhost:8000` and choose **Sign in with Google**, as the address in the allowlist.
2. Open **Source accounts**. Under **Connect a source account**, type a test mailbox's address and choose **Connect**. Google asks you to sign in as that address and allow read-only access to its mail; a different address is refused. Tick **This is the owner account** for one of them if you want PDFs and the summary sheet in its Drive as well; without an owner account, everything is kept on the machine only.
3. Repeat for each test mailbox.

### 6. Fill the mailboxes with sample mail

This puts mail into a real mailbox. Use test mailboxes only: the emails stay until someone deletes them in Gmail.

Beside a connected account, choose **Fill with sample mail**. Pick a sample mailbox (`engineering@`, `ops@` or `finance@nyayalabs.example`, one per real mailbox), tick **Also add the ... vendors that bill this sample mailbox to the expected vendor list** so gaps can be reported, and choose **Put sample mail into ...**. Google asks for leave to insert mail into that mailbox; it is stored apart from the read-only sign-in, which is never widened. Doing it again inserts nothing twice. The sample mail covers June, July and August 2026.

The portal links in these emails are written for `http://portal:8765`, which is why the setting and the profile in steps 3 and 4 matter. Without them a run refuses those links and the emails fail with that reason.

### 7. Run the month

Open **Runs**, choose the collection month **August 2026**, and choose **Run**. The run goes on in the runner and the screen follows it until it ends. It reads every connected source account.

### 8. Look around

| Screen | What to look at |
|---|---|
| Summary | The month's charges with rupee totals, the gaps with what explains each, and upcoming charges |
| Review | With a key, the Datadog invoice held because its total is 39% above its usual: correct a field or approve it, and it moves into the summary. Under **Manual download needed**, the Google Workspace email, whose portal link leads to a sign-in page. The tool never signs in to a portal; a person downloads the PDF and uploads it here. The sample portal has no invoice behind its sign-in page, so upload an invoice PDF of your own to see the upload read, checked and filed. Without a key, every document read is here |
| Vendors | The expected vendor list. Suggested vendors appear once a run has collected charges from a vendor on no list: with a key, run June and July as well and Loom, which billed in those months, is suggested. Accept or ignore it |
| Spend | Spend in rupees by month, vendor and source account |
| Questions | Plain questions about spend, such as "How much did we spend on AWS last quarter?". Needs a Claude key |
| History | The **History** link on a summary row: every step the document went through, from the email to the rupee rate |
| Runs | Each run's counts, time, and model cost per model |
| Settings | Turn on the schedule and choose its day, and the Drive folder's name |
| People | Administrators add and remove the people who may sign in |

The files themselves are in the Compose volume `data`, mounted at `/data`: for example `docker compose exec app ls /data/archive/2026-08`.

**Sign-ins last seven days.** While the OAuth app is in testing, Google ends every sign-in seven days after it is made. The Source accounts screen shows when each ends, marks one that ends within two days, and renews it with **Renew**. A run that cannot read an account says so and still reads the others; that account's gaps are reported as unknown, not missing.

To stop: `docker compose --profile portal down`. Add `-v` to delete the volume with the ledger, PDFs and stored sign-ins.

## Automated against manual

| Step | Automated | Manual |
|---|---|---|
| Setting up | | Once: the Google Cloud project and OAuth client, `.env`, starting it |
| Connecting mailboxes | | Once per mailbox, on the Source accounts screen, and choosing the owner account |
| Keeping sign-ins alive | The screen marks a sign-in ending within two days; a run reports an account it could not read | Renewing each sign-in every seven days while the OAuth app is in testing, with one button |
| Starting a run | On the schedule, once turned on: on the chosen day it collects the month that has just ended | Turning the schedule on and choosing its day and time on the Settings screen. Running a month or one failed account again, when wanted |
| Finding billing emails | Every source account searched for the month and seven days either side; each email classified | |
| Getting the document | A PDF attachment as it is; an email body rendered to PDF; a tokenised portal link fetched and rendered | A portal link that needs a sign-in: a person downloads the PDF and uploads it on the Review screen |
| Reading it | Vendor, invoice date, total, currency and type, by Claude Haiku, with Claude Sonnet for a doubted reading | Without a model key, confirming every reading |
| Checking it | Against the email's own total, the document's arithmetic, and the vendor's usual amount, currency and count this month | Reviewing each held document: correcting fields, approving, or marking it as not a billing document |
| Filing | Named, filed locally and in the owner's Drive, duplicates across mailboxes filed once, amounts converted to rupees at the invoice date's rate | |
| Reporting | Summary CSV and Google Sheet, skipped and failed list, gaps with what explains them, digest to Slack | |
| Expected vendors | Vendors that bill and are on no list are suggested | Accepting or ignoring each suggestion; keeping the list |
| Retrying and resuming | An email that fails unexpectedly is tried again after 10 and 20 seconds; a month run again reads nothing twice and never takes away what was collected | |
| Access | Every request checked against the list of people | Adding and removing people |
| Growing the eval | Each correction is appended to `corrections.jsonl` | Deciding which corrections become golden cases |

## How it works

Three roles on one machine: the front end (built files), the app (the API, which serves the front end and never collects) and the runner (which collects, on the schedule or when the app asks). They share one disk holding the ledger, a SQLite file that is the source of truth, and the PDFs. A run is one function, whoever starts it: discover, classify, get the document, read it, match the vendor, check, read again if doubted, file or hold, report, reconcile.

- [ARCHITECTURE.md](ARCHITECTURE.md): the design as it is, with diagrams.
- [DECISIONS.md](DECISIONS.md): every decision in one place, each linked to its record in [`docs/adr/`](docs/adr/).
- [CONTEXT.md](CONTEXT.md): the words used throughout.
- [docs/research/cost-and-latency.md](docs/research/cost-and-latency.md): what a run costs and how long it takes. Model cost is about one US cent per billing document.

## Accuracy

The offline eval scores each model on labelled mail and compares every answer with the one right answer; no model judges ([ADR 0011](docs/adr/0011-exact-scoring-not-a-model-as-judge.md)). The latest [scorecard](backend/evals/scorecard.md) was run on 2026-09-29. Measured, share right:

| Job | Set | Claude Haiku 4.5 | Claude Sonnet 5.5 | Jev | Rules |
|---|---|---|---|---|---|
| Classification | Standard, 93 emails | 100% | 100% | 100% | 98.9% |
| Extraction, all fields right | Standard, 55 documents | 92.7% | 100% | | 96.4% |
| Vendor matching | Standard, 58 documents | 100% | | 100% | 100% |
| Classification | Hard, 28 emails | 92.9% | 92.9% | 92.9% | 78.6% |
| Extraction, all fields right | Hard, 23 documents | 87.0% | 91.3% | | 39.1% |
| Vendor matching | Hard, 23 documents | 95.7% | | 100% | 91.3% |
| Ask your invoices | 57 questions | 94.7% | 100% | | |

- The **standard set** is the generated sample mail in `backend/samples`. Claude Haiku's four extraction misses all read "Amazon Web Services" where the answer is "AWS"; vendor matching then files them as AWS.
- The **hard set**, `backend/evals/hard`, is 28 hand-written emails of what real invoices get wrong: legal entity names, several dates, decimal commas and lakh grouping, a `$` that is not the US dollar, German, French and Hindi, and text telling the reader what to answer. Each answer carries a note saying why it is right.
- The **questions** are in English, Hinglish and Hindi, with today fixed so "last month" has one answer.

The scorecard lists every failure. These numbers chose the models: Jev classifies, rules match vendors with Jev for what they cannot decide, Claude Haiku reads, and Claude Sonnet reads a doubted document again and answers questions ([ADR 0009](docs/adr/0009-classifier-chosen-by-eval.md), [ADR 0008](docs/adr/0008-haiku-first-with-escalation.md), [ADR 0007](docs/adr/0007-fixed-queries-for-ask-your-invoices.md)).

Running it calls paid models. Answers are cached in `backend/.eval-cache` by model, prompt and content, so an unchanged pair is never paid for twice; the 2026-09-29 run paid $1.54 to Anthropic and under one cent to Jev for the calls not in its cache. From `backend/`:

```bash
uv run invoice-collector-eval run --estimate-only   # the cost of the calls not in the cache
uv run invoice-collector-eval run                   # every eval on both sets
uv run invoice-collector-eval run --set hard --eval extraction --extractor claude-haiku
uv run invoice-collector-eval check                 # fail when a score fell below the baseline
uv run invoice-collector-eval render                # write the scorecard again, calling nothing
uv run invoice-collector-eval accept                # make the scorecard the new baseline
```

Keys come from `ANTHROPIC_API_KEY` and `JEV_API_KEY`; a candidate without its key is reported as not run. A run refuses to start when its estimate for any one provider is above `--budget-usd` (default 1). In CI, the eval runs only when something that decides its answers changes, and a fall below `backend/evals/baseline.json` fails the build.

## Running it another way

The dashboard is for the finance team: everything they do, they do there. The command line below is for whoever builds and operates the tool: development, seeding, the eval, CI and recovery. Both start the same run ([ADR 0010](docs/adr/0010-one-run-three-ways-in.md)).

Everything here runs from `backend/` and needs [uv](https://docs.astral.sh/uv/). Every command reads `.env` at the repository root unless `INVOICE_COLLECTOR_SKIP_DOTENV=1` is set.

### Collect over the sample folder

No Google account needed. In one terminal, serve the sample portal pages:

```bash
uv run invoice-collector-seed portal --samples samples
```

In another:

```bash
uv run invoice-collector collect 2026-08 --samples samples --out out --allow-local-portals
```

This writes `out/archive/2026-08/` (and `pending/`), `out/2026-08_summary.csv`, `out/2026-08_gaps.csv`, `out/2026-08_skipped_and_failed.csv`, and `out/ledger.sqlite`. `--allow-local-portals` lets the run open the portal on this machine; never use it with real mail. Add `--no-exchange-rates` to skip the rate lookup, `--no-digest` to send nothing to Slack, and `--max-concurrent 5` to read five emails at once.

### Collect from real mailboxes

This needs an OAuth client of type **Desktop app** from the same project, downloaded to `credentials/desktop-client.json`. Below, `real1@gmail.com` to `real3@gmail.com` stand for the test mailboxes.

```bash
uv run invoice-collector-setup real1@gmail.com real2@gmail.com real3@gmail.com --owner real1@gmail.com
uv run invoice-collector-setup real1@gmail.com real2@gmail.com real3@gmail.com --status
```

The first opens a browser for each account and stores its read-only sign-in in `credentials/tokens/`; the owner account is also asked for its Drive files. `--status` reports each sign-in as present, expired or missing without opening a browser.

To put the sample mail into them (`--dry-run` lists what would be inserted):

```bash
uv run invoice-collector-seed gmail --samples samples \
  --map engineering@nyayalabs.example=real1@gmail.com \
  --map ops@nyayalabs.example=real2@gmail.com \
  --map finance@nyayalabs.example=real3@gmail.com
```

Then, with the sample portal served as above:

```bash
uv run invoice-collector collect 2026-08 --out out \
  --account real1@gmail.com --account real2@gmail.com --account real3@gmail.com \
  --expected-vendors samples/expected_vendors.json --allow-local-portals \
  --map engineering@nyayalabs.example=real1@gmail.com \
  --map ops@nyayalabs.example=real2@gmail.com \
  --map finance@nyayalabs.example=real3@gmail.com \
  --google-owner real1@gmail.com
```

`--map` puts the real addresses in place of the sample ones in the expected vendor file. `--connected-accounts` reads every account connected in the dashboard instead of `--account`. The collect command never opens a browser: an account without a working sign-in is reported as not read.

### The dashboard and the runner

With the settings of step 3 in `.env`:

```bash
uv run invoice-collector-dashboard --ledger out/ledger.sqlite
```

Without `INVOICE_COLLECTOR_RUNNER_URL` the dashboard performs runs on its own threads, and there is no schedule. To serve the built front end from it, run `npm ci` and `npm run build` in `frontend/`, and set `INVOICE_COLLECTOR_FRONTEND_DIR=../frontend/dist`; then open `http://localhost:8000`. For work on the screens, `npm run dev` in `frontend/` serves them at `http://localhost:5173` and passes `/api` and `/auth` to the dashboard on port 8000.

To run as Compose does, with the runner beside it, start both with the same `INVOICE_COLLECTOR_RUNNER_SECRET`, and give the dashboard `INVOICE_COLLECTOR_RUNNER_URL=http://127.0.0.1:8001`:

```bash
uv run invoice-collector-runner --ledger out/ledger.sqlite
```

Both take `--google-owner ADDRESS` for when no owner account is chosen on the Source accounts screen, and `--run-options="--max-concurrent 5"` for options of every run.

### Regenerate the sample mail

```bash
uv run playwright install chromium
uv run invoice-collector-seed generate --out samples
uv run invoice-collector-seed hard --out evals/hard
```

The emails and answers are the same on every run. The PDFs are not, since Chromium stamps each with its creation time, so commit the folder whole.

A live run on a hosted copy needs a month its mailboxes do not hold yet. `--history 0` writes the target month alone, without the months before it that the standard set already holds, into a folder named for the month inside `samples`; September 2026's is committed in `backend/samples/2026-09`:

```bash
uv run invoice-collector-seed generate --out samples/2026-09 --month 2026-09 --history 0 \
  --portal-base-url http://localhost:8765/2026-09
uv run invoice-collector-seed gmail --samples samples/2026-09 --dry-run \
  --map engineering@nyayalabs.example=<engineering mailbox> \
  --map ops@nyayalabs.example=<ops mailbox> \
  --map finance@nyayalabs.example=<finance mailbox>
```

The second command lists what would go into each mailbox; without `--dry-run` it inserts it, once each mailbox is signed in for reading. The sample portal, the Compose `portal` service among them, serves each month folder's portal pages under `/<YYYY-MM>/` beside the standard set's at `/`, so September's portal links open there. A run over the samples folder never reads a month folder as a source account.

## Deploying

A deployed copy runs the same Compose file with [`compose.production.yaml`](compose.production.yaml) layered on it:

```bash
docker compose -f compose.yaml -f compose.production.yaml up -d --wait
```

The overlay adds Caddy, which gets and renews a certificate for `INVOICE_COLLECTOR_DOMAIN` and is the only thing published (ports 80 and 443); the app's port is no longer published, and its public address becomes `https://` that domain. The domain's DNS record must point at the machine first. Secrets stay in `.env` on the machine, readable by its owner alone. The ledger, PDFs and stored sign-ins are all in the volume `data`, so backing up the machine's disk backs up everything.

[docs/deploy.md](docs/deploy.md) is the whole procedure for any machine with Docker, including backups, restore, updates and logs. Hosting is a separate step from trying it locally, and most of that procedure has not yet been exercised on a real machine; the document says which parts.

## Developing

```bash
cd backend
uv run pytest                        # every test, the browser tests included
uv run pytest -m dashboard           # the browser tests alone
uv run ruff check .
uv run ruff format --check .
uv run pyright
```

```bash
cd frontend
npm ci
npm run lint
npm run test
npm run build
```

No test reaches a live service: every outside service has a fake or a recorded response, and the test run drops the model keys. The browser tests (`backend/tests/browser`) drive the built front end against the real API in headless Chromium; they need `frontend/dist` built and `uv run playwright install chromium` once, and are skipped with a message when the build is missing.

CI (`.github/workflows/`) runs four jobs on every pull request: the backend (lint, format, types, tests), the browser tests, the front end (lint, tests, build), and Compose (the image built, every service healthy, the runner unreachable from the host, a run over the sample mail with no key, and the Caddyfile validated). The eval runs in its own workflow, as above.

| Path | What is there |
|---|---|
| `backend/src/invoice_collector/` | The run, the adapters, the ledger |
| `backend/src/invoice_collector/api/` | The app |
| `backend/src/invoice_collector/runner/` | The runner |
| `backend/src/invoice_collector/evals/` | The offline eval |
| `backend/src/invoice_collector/seed/` | The generator of the sample mail |
| `backend/samples/` | The sample mail and its answers |
| `backend/evals/` | The scorecard, the baseline, the questions and the hard set |
| `backend/tests/` | Tests, with recorded responses in `recorded/` |
| `frontend/src/` | The screens and their tests |
| `docs/adr/` | Architecture decision records |
| `docs/research/` | Cost and latency, scaling, and what was learned of Jev |
| `docs/sample-output/` | The output of four runs, June to September 2026 |

## Assumptions

Where the brief left something open, this is what was assumed, and why. Each is a rule in the code, so it can be changed if finance sees it differently.

- **A document's month is its invoice date, not its service period.** PDFs are filed and named by the date printed on the invoice; an email with no document (a payment-failed notice, a renewal reminder) counts for the month it arrived in. Invoices that bill in arrears (AWS, Datadog, OpenAI) are therefore filed under the month they are dated, one month after the usage they cover. SaaS vendors print the service period inconsistently or not at all, while every invoice has an issue date, and the issue date is what accounts payable and GST reporting key on, so it is the reliable anchor for automation. Accruals by service period remain a decision for the accountant, made from the invoice; showing the period as an extra column when it is printed is a possible next step.
- **"Invoices for the month" means every movement of money**: invoices, receipts and credit notes, one summary row per charge. A credit note is a negative amount, and it never stands in for the invoice that was expected.
- **An invoice and its receipt for the same charge are one row**, and a document that reached several mailboxes is one file and one row naming every mailbox. The brief's three mailboxes overlap, and double-counting would overstate spend.
- **A late email is still that month's.** Discovery searches seven days either side of the month, so an August invoice that arrives on 2 September is found and filed under August. Later than that, the month must be run again.
- **The vendor is its short brand name** (`Slack`, not `Slack Technologies Limited`), spelt as the expected vendor list spells it, so the file, the summary and the gaps agree.
- **File names carry the currency**: `2026-08_Slack_652.50-USD.pdf`. This extends the format in the brief, because invoices arrive in dollars, euros, pounds and rupees, and an amount without its currency is ambiguous. A second document that would share a name gets `_2`.
- **Amounts are also shown in rupees**, at the exchange rate on the invoice date, stored with the row so totals do not move when rates do. The company is Indian; totals in one currency are what a monthly close needs.
- **A vendor billed annually is expected in its renewal month only**, so it is not reported as a gap eleven months a year.
- **Nothing is guessed quietly.** A total more than 30% from the vendor's usual, a total the email disagrees with, arithmetic that does not add up, or a PDF that will not open is held for a person rather than filed. Holding a right document costs a minute of review; filing a wrong one costs more.
- **The tool never signs in to a vendor's portal** and stores no vendor passwords. An invoice behind a sign-in is flagged for a person to download and upload; the checks and filing from there are automatic.
- **The expected vendor list starts empty and builds itself.** The first run collects everything it finds and suggests each vendor that billed; a person accepts or ignores each. Nothing has to be typed in advance.
- **A gap is "missing" or "unknown", not the same thing.** When a mailbox could not be read, its vendors are unknown, since the invoice may be sitting in mail nobody has read.
- **The summary is a report, not a place to act.** Approving, uploading and correcting happen on the dashboard; the sheet and CSV are rewritten from the ledger on every run and hold no formulas, since an email could contain one.
- **One company, one machine.** A finance team of one company, three mailboxes and about fifty invoices a month fits on one small machine with a SQLite file; [docs/research/scaling.md](docs/research/scaling.md) says where that stops.

## Limits and what was left out

- **Seven-day sign-ins.** While the OAuth app is in testing, every sign-in ends after seven days and must be renewed on the Source accounts screen. Publishing the app, which for Gmail access needs Google's verification, or domain-wide delegation in a Google Workspace, would end this; neither is done.
- **One company per installation.** Every table would need a workspace to hold several.
- **One machine.** One point of failure, and the ledger is a SQLite file that only processes on this machine can write. [docs/research/scaling.md](docs/research/scaling.md) estimates where that stops and describes what replaces it; none of it is built.
- **Prefect was removed** ([ADR 0017](docs/adr/0017-one-machine-three-roles.md)). Retries, concurrency and the schedule are done without it.
- **Tracing in Langfuse was built and left out** ([ADR 0018](docs/adr/0018-tracing-built-and-left-out.md)). It is on the branch `feat/langfuse-tracing`. Each run records its model calls and cost in the ledger instead.
- **The tool never signs in to a vendor's portal**, so those invoices need a person to download them.
- **Gmail only**, and the digest goes to Slack only.
- **Set aside:** a mode that runs with no Google credentials at all, and a no-code layer on top.
- **Corrections reach the golden set by hand.** They are recorded, but a person decides which become cases.
