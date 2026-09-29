# Decisions

Every decision made in building this tool, in one place. Each is stated briefly. Where the reasoning needs more room, the entry links to an architecture decision record (ADR) in `docs/adr/`.

Terms such as billing document, source account and gap are defined in [CONTEXT.md](CONTEXT.md).

## How to read this

| Section | What it covers |
|---|---|
| [Approach](#approach) | What kind of thing this is, and what it is built with |
| [Models](#models) | Which model does which job, and how that was chosen |
| [What is collected](#what-is-collected) | The rules for billing documents, months and charges |
| [Trust and safety](#trust-and-safety) | What the tool refuses to trust, and what it refuses to do |
| [When things go wrong](#when-things-go-wrong) | Failures, doubts and re-runs |
| [Outputs](#outputs) | The archive, the summary, gaps and the digest |
| [The dashboard](#the-dashboard) | What people do, and who may do it |
| [Quality](#quality) | Tests and the eval |
| [Running it](#running-it) | The command line, the schedule and deployment |
| [Process](#process) | How the work was done |
| [Considered and not done](#considered-and-not-done) | What was weighed and turned down |
| [Still open](#still-open) | What is not yet decided |

## Approach

| Decision | In short | More |
|---|---|---|
| Code, not a no-code workflow tool | The hard part is getting financial data right, which needs tests, reviewable changes and an audit trail. Connecting Gmail to Drive is the easy part. | [ADR 0001](docs/adr/0001-code-over-no-code.md) |
| Google's own APIs, with our own OAuth app | Sign-ins for finance mailboxes stay on our machine and are not held by a third party. | [ADR 0002](docs/adr/0002-direct-google-apis-over-managed-connectors.md) |
| Read-only access to mail | The tool cannot send, change or delete mail. Only the owner account is also given access to the Drive files the tool itself creates. | [ADR 0002](docs/adr/0002-direct-google-apis-over-managed-connectors.md) |
| Python | The strongest tooling for PDFs and for the workflow orchestrators worth using. | |
| A workflow orchestrator, not an agent framework | The pipeline is a fixed sequence with one routing decision. Nothing in it needs a model to decide what happens next. Prefect supplies retries, a cap on concurrent calls, and a schedule. | [ADR 0003](docs/adr/0003-workflow-orchestrator-over-agent-framework.md) |
| Core logic has no orchestrator in it | Prefect is a thin layer that calls plain functions, so tests of the run never touch it and it can be replaced. | [ADR 0003](docs/adr/0003-workflow-orchestrator-over-agent-framework.md) |
| Emails are read at once and decided one at a time | Under Prefect, fetching and reading run concurrently. Weighing a document against the ledger and recording it happen one email at a time, so the checks give what a run one email at a time gives. | [ADR 0003](docs/adr/0003-workflow-orchestrator-over-agent-framework.md) |
| A pipeline of stages | Discover, classify, route by invoice format, extract, check, store, report, reconcile. | |
| The ledger is the source of truth | The summary, the archive and the dashboard are all views of it. It is a SQLite file, behind an interface so Postgres can replace it. | [ADR 0006](docs/adr/0006-dashboard-is-the-only-action-surface.md) |
| Every module that touches the outside world has a fake | Mail, models, the browser, exchange rates, Drive, Sheets and Slack can each be replaced in a test. | |

## Models

| Decision | In short | More |
|---|---|---|
| Claude Haiku 4.5 reads billing documents | Extraction is reading, not reasoning. The smallest current Claude model is sufficient, fast and cheap. | [ADR 0008](docs/adr/0008-haiku-first-with-escalation.md) |
| The PDF itself is sent to the model | One extraction path serves all three invoice formats. | [ADR 0008](docs/adr/0008-haiku-first-with-escalation.md) |
| A doubted reading goes to Claude Sonnet 5.5 | Only a doubt about the reading is worth a second reading. A total that is merely unusual was read correctly as far as anyone knows. | [ADR 0008](docs/adr/0008-haiku-first-with-escalation.md) |
| Rules are the fallback, and what they read is always held | When no model can be used, strict rules read the document, and mark their own reading as unsure so a person confirms it. | [ADR 0008](docs/adr/0008-haiku-first-with-escalation.md) |
| Jev classifies emails | It tied with Claude Haiku at 100% on the eval, at about a twenty-eighth of the cost and a third of the time. Claude Haiku, then rules, stand behind it. | [ADR 0009](docs/adr/0009-classifier-chosen-by-eval.md) |
| Rules match vendors, with Jev for what rules cannot decide | All three candidates were right every time, so the one that is free, offline and repeatable goes first. | [ADR 0009](docs/adr/0009-classifier-chosen-by-eval.md) |
| Vendor matching falls back as classification does | Jev when its key is set, then Claude Haiku, then rules alone. A model that fails leaves the rules' answer, with a warning; it never fails the email. | [ADR 0016](docs/adr/0016-expected-vendor-spelling-wins.md) |
| A model's own confidence is not relied on | In the eval every model rated itself confident on every answer. Holding for review rests on checks instead. | [ADR 0004](docs/adr/0004-evals-from-labelled-seed-data.md) |
| The model never writes a database query | For questions about spend, the model chooses one of a fixed set of queries. The server runs it and writes the answer. The model never sees an amount. | [ADR 0007](docs/adr/0007-fixed-queries-for-ask-your-invoices.md) |
| Model names are settings | The default can change without a release. | |

What each job costs and how long it takes is worked through in [docs/research/cost-and-latency.md](docs/research/cost-and-latency.md). Model cost is about one US cent per billing document.

## What is collected

| Decision | In short |
|---|---|
| One document per movement of money | Invoices, receipts and credit notes are collected. One summary row per charge. |
| A credit note has a negative amount | So monthly totals are right. |
| Billing signals are recorded, not collected | A payment-failed notice or a renewal reminder moves no money, but it can explain a gap. |
| An invoice and its receipt are one charge | The invoice is the file. The row notes that a receipt was also received. |
| A document in several source accounts is one charge | One file and one row, which lists every source account it was found in. |
| The invoice date decides the month | Not when the email arrived. Discovery looks seven days either side of the month so a late email is not missed. |
| An email dated for another month is left for that month | It is skipped with the reason "belongs to collection month 2026-07", so it is visible. |
| The vendor is its short brand name | "Slack", not "Slack Technologies Limited". |
| A document takes the expected vendor's spelling | When the vendor it names matches one on the expected list, it is filed, summarised and checked under the list's spelling. What it said is kept in the ledger. See [ADR 0016](docs/adr/0016-expected-vendor-spelling-wins.md). |
| The vendor is matched from the document's text | As the eval scored it; from the email's text when the PDF has none. A name that differs only in case, punctuation or a legal suffix needs no matcher. |
| A document collected under another name is renamed on the next run | In the ledger only. Its PDF keeps its file and file name, and no duplicate is made. See [ADR 0016](docs/adr/0016-expected-vendor-spelling-wins.md). |
| A name a person confirmed is not matched again | A person's decision is final. |
| A suggestion no charge supports is withdrawn | It was the run's guess from a name that has since been matched. |
| The filename carries the currency | `2026-08_Slack_652.50-USD.pdf`. This adds the currency to the format in the brief, because amounts in mixed currencies are ambiguous without it. |
| A name clash gets a suffix | `_2`, `_3`. A different document is never overwritten. |
| Amounts are also shown in rupees | At the rate on the invoice date. The rate is stored with the row so totals do not move when rates do. |

## Trust and safety

| Decision | In short | More |
|---|---|---|
| What an email contains is not trusted | Bodies are rendered with scripts off and no network access. | [ADR 0012](docs/adr/0012-email-content-is-untrusted.md) |
| Where a link leads is not trusted | Only secure links to public addresses are opened, and the address is checked again at the moment of connecting. | [ADR 0012](docs/adr/0012-email-content-is-untrusted.md) |
| A link is never followed on a guess | It must mention an invoice, receipt, billing or statement. An unsubscribe link is never opened. | [ADR 0012](docs/adr/0012-email-content-is-untrusted.md) |
| The tool never signs in to a vendor's portal | It stores no vendor passwords and never submits a form. A link behind a sign-in is flagged for a person to download. | [ADR 0012](docs/adr/0012-email-content-is-untrusted.md) |
| An uploaded file is judged by its content | It must be a readable PDF without a password, whatever it is called, and at most 20 MB. It is never rendered, and the text in it is only read as data. | [ADR 0012](docs/adr/0012-email-content-is-untrusted.md) |
| Text from an email cannot run as a formula | Cells that begin with a formula character are written as text. | [ADR 0012](docs/adr/0012-email-content-is-untrusted.md) |
| The audit trail holds facts about an email, not its content | Sender, subject, date and account, never the body. Of a portal link only the host is kept, since a tokenised link opens the vendor's billing page to whoever holds it. Everything is shown as text. | [ADR 0012](docs/adr/0012-email-content-is-untrusted.md) |
| Secrets are never in the repository | Keys, client files and stored sign-ins live in git-ignored places and are read from the environment. | |
| Seeding uses its own sign-in | Putting sample mail into a mailbox needs write access, which is stored apart so it never widens what the pipeline can do. | |

## When things go wrong

| Decision | In short | More |
|---|---|---|
| Nothing is dropped and nothing is guessed | Every email examined ends in exactly one state: collected, needs review, skipped, or failed with a reason. | [ADR 0005](docs/adr/0005-no-silent-drops-no-guesses.md) |
| A failure is contained | One email failing, or one source account being unreadable, does not stop the run. | [ADR 0005](docs/adr/0005-no-silent-drops-no-guesses.md) |
| The run retries an email itself | An examination that raises something unanticipated, such as a dropped connection, is tried again after 10 and then 20 seconds, then recorded as failed with the error. A fault in the code is not retried. This lives in the run, so the command line has it and tests reach it without Prefect; under Prefect, each task is retried by the same rule instead. | [ADR 0005](docs/adr/0005-no-silent-drops-no-guesses.md) |
| A doubtful document is held for a person | It stays out of the summary, and its PDF sits in a pending folder, until someone confirms it. | [ADR 0004](docs/adr/0004-evals-from-labelled-seed-data.md) |
| A PDF that cannot be opened is held as it is | A damaged or password-protected PDF that no reader could read is saved to the pending folder byte for byte and held. Its fields start from the email, the sender and the day it arrived, with no amount (0.00 in XXX, the code for no currency), every field marked for the person to enter. A PDF that opens but that nothing could read still fails, since a later run may read it. | [ADR 0005](docs/adr/0005-no-silent-drops-no-guesses.md) |
| What raises a doubt | The email states a different total; subtotal and tax do not add up; the total is more than 30% from the vendor's usual; the currency is not the vendor's usual; it is the vendor's second invoice this month; the reader was unsure. | [ADR 0004](docs/adr/0004-evals-from-labelled-seed-data.md) |
| A run never takes away what was collected | If a model is down or answers differently on a second run, what was collected before stays. | [ADR 0013](docs/adr/0013-a-run-never-takes-away.md) |
| A document is known by its source | Not by its PDF, which differs on every rendering. A known document is not fetched or read again. | [ADR 0013](docs/adr/0013-a-run-never-takes-away.md) |
| An email with billing documents is not classified again | Once a run has collected or held documents from an email, a later run looks for them straight away without asking the classifier. A crashed run started again redoes only what it had not finished. Failed and skipped emails are examined afresh, since a later attempt may succeed. | [ADR 0013](docs/adr/0013-a-run-never-takes-away.md) |
| An uploaded document is known by its portal link | The identity a run gives a document behind that link, so a later run neither opens the link again nor takes the upload away. The bytes are recognised too: the same file uploaded again changes nothing, and a file already collected as an attachment is linked to that document, not filed twice. | [ADR 0013](docs/adr/0013-a-run-never-takes-away.md) |
| A person's decision is final for the run | An email marked as not a billing document is not held again next time. | [ADR 0013](docs/adr/0013-a-run-never-takes-away.md) |
| Every run is recorded in the ledger | How it was started, when it started and finished, how many of the emails it examined were collected, need review, were skipped or failed, and model cost when known (not yet metered). A run that crashes stays unfinished. This is what a Runs screen shows. | |
| Whether an account could be read is kept per run | The syncs table has a row each time a run reads a source account, so a run keeps its failures after a later run reads the account. Gaps go by the latest row. A ledger from before is rebuilt with its rows kept. | |
| A ledger from an earlier version keeps working | Missing columns are added when it is opened. | |

## Outputs

| Decision | In short | More |
|---|---|---|
| The summary is a read-only report | The brief offers CSV as an equal alternative, and nobody takes actions in a CSV. The tool rewrites it on every run. | [ADR 0006](docs/adr/0006-dashboard-is-the-only-action-surface.md) |
| Google Drive and Sheets, with a local copy | PDFs go to Drive and to a local folder. The summary goes to a Google Sheet and to a CSV. | |
| One folder per collection month | Under one root folder, with a pending subfolder. | |
| Skipped and failed emails are listed beside the summary | With their reasons, in the sheet's tab of that name and in a CSV beside the local summary, so a run without Google has the list too. Source accounts that could not be read come first. Failed emails are also printed. | |
| The summary holds confirmed documents only | Pending ones are in their own tab, each linking to that item in the dashboard. | [ADR 0006](docs/adr/0006-dashboard-is-the-only-action-surface.md) |
| A gap is missing or unknown | Unknown when the vendor's source account could not be read, since the invoice may be in mail nobody has read. | [ADR 0005](docs/adr/0005-no-silent-drops-no-guesses.md) |
| A document held for review explains its vendor's gap | The gap stays, since nothing is confirmed, and says "held for review" with the doubt. It is not a kind of gap of its own: missing and unknown say whether the mail was read, and an explanation says why nothing was collected, as a failed payment does. | |
| A vendor billed annually is expected in its renewal month only | So it is not reported as a gap eleven months a year. | |
| A credit note does not stand in for an invoice | Money returned is not the invoice that was expected. | |
| Vendors are suggested, not assumed | A vendor that has billed and is on no list is suggested. It is expected only once a person accepts it. | |
| The digest goes to Slack | Not email, so no mailbox needs permission to send. It says what was collected, the gaps, and what needs review. | |
| "Not checked" is different from "none" | A digest with no gap check says so, and does not claim there are no gaps. | |

## The dashboard

| Decision | In short | More |
|---|---|---|
| Every action happens in the dashboard | See everywhere, act in one place. The sheet shows what is pending; the dashboard is where it is confirmed. | [ADR 0006](docs/adr/0006-dashboard-is-the-only-action-surface.md) |
| FastAPI and React | A Python API with a React front end, and no UI library. | |
| Review is three panes | The queue, the document, and the extracted fields. Chosen from three prototypes. | |
| Review decides a whole email | Approve, with every held document of the email confirmed together, or mark it as not a billing document. All or nothing for the email. | |
| A document in several source accounts is one entry in the review queue | A decision applies to every email holding it. | |
| An approved document is filed under the name its confirmed fields give it | Its rupee amount is looked up again. A later run knows it by content and does not read it again. | [ADR 0013](docs/adr/0013-a-run-never-takes-away.md) |
| An approved document is filed where the run files one | Given the owner account, the dashboard files it to Drive first and to the local archive, as a run with that owner does. If Drive cannot be reached, nothing changes and the email stays held. | |
| The pending copy goes once a decision is made | After approval or rejection is recorded, the copy in the pending folder is removed from each archive it was filed to. In Drive it is moved to the bin, so it can be restored. A copy that cannot be removed is reported on the Review screen, and the decision stands. | |
| A decision is recorded before anything is tidied up | The ledger is written, then the review history and any corrections, and only then are the pending copies removed. Whatever the removal meets, of any kind, is a warning on the Review screen and never a failed request, so a decision is never left made but unrecorded. | [ADR 0005](docs/adr/0005-no-silent-drops-no-guesses.md) |
| A held document's local copy is known by the SHA-256 of its PDF | The run records the SHA-256 of the PDF it saves for each held document. When the ledger links the document to Drive, the Review screen takes the local file of its name, or that name numbered, with that SHA-256, so two held documents with the same fields and different PDFs are each filed with their own. The content hash cannot serve: for a body or a portal page it is the hash of that source, not of the PDF. | [ADR 0013](docs/adr/0013-a-run-never-takes-away.md) |
| A held document from an older ledger is not guessed | A ledger written before the SHA-256 was recorded names no file. The only file of the document's name is taken; with several, approval is refused naming them, and nothing changes. | [ADR 0005](docs/adr/0005-no-silent-drops-no-guesses.md) |
| An email that is not a billing document is recorded as skipped | With that reason, and its pending PDF is deleted. A later run does not examine it again. | [ADR 0013](docs/adr/0013-a-run-never-takes-away.md) |
| A document that needs a manual download is listed apart | With its portal link. It cannot be approved or rejected on the Review screen; the PDF downloaded from the link is uploaded there instead. | |
| An assisted download is uploaded on the Review screen | Beside the email that carried the portal link, not on a screen of its own, since the Review screen already lists these emails and is where held documents are decided. | [ADR 0006](docs/adr/0006-dashboard-is-the-only-action-surface.md) |
| An upload is read and checked as a run reads a document | The same extractors, a stronger model for a doubted reading, and the same checks. Nothing doubted: filed and reported. A doubt: held for review with the reasons, and decided like any other held document. | [ADR 0004](docs/adr/0004-evals-from-labelled-seed-data.md) |
| An upload is filed under the month of its invoice date | As a run files any document. Where the dashboard has the owner account's Drive, it is filed there first. | |
| One upload settles every email with the same portal link | The same link in several source accounts is one document, as for an approval. | |
| An upload that cannot be read changes nothing | A file that is not a billing document, or that no reader could read, is refused and the email stays flagged, since the person is there to try another file. | [ADR 0005](docs/adr/0005-no-silent-drops-no-guesses.md) |
| Every upload is recorded | Who uploaded it, when, the size and hash of the file, and what became of it, in its own table in the ledger file, for the audit trail. | |
| Every review decision is recorded | Who, when, and each field before and after. Corrections are appended to `corrections.jsonl` beside the ledger for the golden dataset. | [ADR 0004](docs/adr/0004-evals-from-labelled-seed-data.md) |
| Source accounts are connected in the dashboard | A person in finance connects a mailbox without a command line. | [ADR 0014](docs/adr/0014-source-accounts-and-people-in-the-dashboard.md) |
| The address that signed in must be the one being connected | Otherwise nothing is stored. | [ADR 0014](docs/adr/0014-source-accounts-and-people-in-the-dashboard.md) |
| People are managed in the dashboard | Administrators add and remove people. Members do everything else. | [ADR 0014](docs/adr/0014-source-accounts-and-people-in-the-dashboard.md) |
| The setting is the way back in | Addresses in the setting are administrators who cannot be removed from the dashboard, so nobody can be locked out by a mistake made in it. | [ADR 0014](docs/adr/0014-source-accounts-and-people-in-the-dashboard.md) |
| Every change is recorded | Who corrected which field, accepted which vendor, or connected which account, and when. | |
| Every billing document has an audit trail | Reached from its summary row and from the Review screen: a timeline from the email arriving to the rupee rate, with the classifier, the reading model, the checks, and each correction. | |
| The trail is recorded as it happens | A run and the Review screen add events to a `document_events` table in the ledger at the moment of each step, since the other tables keep only the latest state. The table is added when an earlier ledger is opened. | |
| A document from before the trail has a shorter one | Its emails, where it was filed and its rate are filled in from the latest state, without times, and the page says the history is short. | |
| The kind of a trail event is open | A kind the dashboard does not know is shown by its name and details, so vendor matching, run records and uploads can add theirs without changing the trail. | |
| Classifications and readings name what produced them | The model's name, or "rules", travels with each answer, because behind the fallbacks it is otherwise lost. | [ADR 0008](docs/adr/0008-haiku-first-with-escalation.md) |
| Each check that ran is reported, not only its doubts | So the trail can show a check that ran and passed. A check with nothing to compare, such as a total the email does not state, did not run. | |
| A re-run adds nothing to the trail when nothing changed | An event the same as the latest of its kind for that email and document is not added again. | [ADR 0013](docs/adr/0013-a-run-never-takes-away.md) |
| The trail shows a vendor match that changed the name | With the name as read, the expected vendor, and what matched it: "rules" for a name that differs only in case, punctuation or a legal suffix or that the rules placed, otherwise the model's name. A name read as the list spells it adds no step. | [ADR 0016](docs/adr/0016-expected-vendor-spelling-wins.md) |
| A retry is a step, and the attempt that raised leaves no other | Nothing of an attempt that raised is recorded, so its partial steps are dropped too; each retry is recorded with the error that caused it, before the steps of the attempt that completed. | [ADR 0005](docs/adr/0005-no-silent-drops-no-guesses.md) |
| A PDF that cannot be opened is a step of its own | Saying it was saved as it is, with the problem. No rate to rupees is recorded for it, since nothing was read. | [ADR 0005](docs/adr/0005-no-silent-drops-no-guesses.md) |
| Removing the pending copy after a decision is a step | By the person who decided, at the time of the decision. A copy that could not be removed is a step too, with the warning the Review screen gave. | |

## Quality

| Decision | In short | More |
|---|---|---|
| Tests sit at three seams | The run, the dashboard's API, and the dashboard in a browser. Each checks what a user would see, not how it was produced. | |
| No test reaches a live service | Every test run drops the model keys, whatever the machine holds. | |
| Connections are tested against recorded responses | Replayed by a local server. | |
| Accuracy is measured by an eval, not assumed | The sample invoices are generated with their right answers, which gives a golden dataset. | [ADR 0004](docs/adr/0004-evals-from-labelled-seed-data.md) |
| Exact scoring, with no model as judge | Every answer has one right value, so the eval compares and does not judge. | [ADR 0011](docs/adr/0011-exact-scoring-not-a-model-as-judge.md) |
| A fall in accuracy fails the build | The eval runs in CI only when extraction, classification or matching changes. | [ADR 0004](docs/adr/0004-evals-from-labelled-seed-data.md) |
| Eval answers are cached | An unchanged document and prompt are never paid for twice. | |
| Corrections feed the golden dataset | What a person corrects in review is recorded, so the eval grows from real mistakes. | [ADR 0004](docs/adr/0004-evals-from-labelled-seed-data.md) |

## Running it

| Decision | In short | More |
|---|---|---|
| One run, three ways in | The schedule, the dashboard and the command line all start the same function. | [ADR 0010](docs/adr/0010-one-run-three-ways-in.md) |
| The command line is for engineers | Development, seeding, the eval, CI and recovery. Finance never needs it. | [ADR 0010](docs/adr/0010-one-run-three-ways-in.md) |
| Deployed once, used by the whole team | Through one address. Whoever deploys sets the secrets. | [ADR 0010](docs/adr/0010-one-run-three-ways-in.md) |
| The deployment is described, not hosted | A hosted copy would stop reading mail after seven days, which is how long Google lets a sign-in live while the OAuth app is in testing. | [ADR 0015](docs/adr/0015-described-not-hosted.md) |
| The pipeline never opens a browser to sign in | An account with no usable sign-in is reported as unreadable. Only the setup command and the dashboard open one, because a person is there. | |
| Sample mail is inserted, not sent | Through the Gmail API, so senders look like the real vendors. | |
| The collect command takes the seed command's `--map` | A sample account in the expected vendor file is replaced by the real address, when the list is filled and on a list an earlier run filled, so gaps name a real mailbox. | |

## Process

| Decision | In short |
|---|---|
| Decisions first, then a spec, then tickets | The spec is [issue 1](https://github.com/itsskofficial/adalatai-assignment/issues/1). Each ticket is a slice that works end to end. |
| Two prototypes before the spec | An extraction spike, and three layouts for the review screen. Both are kept on their own branches. |
| Test first | A failing test, then the code to pass it. |
| One branch and one pull request per ticket or group of related tickets | Reviewed by CodeRabbit. Sound suggestions are fixed. The rest are answered with the reason. |
| Every decision is recorded | Here, and in an ADR where the reasoning needs room. |

## Considered and not done

| What | Why not | More |
|---|---|---|
| n8n or another no-code tool | It makes the easy part trivial and the hard part harder. It would be the better choice if finance maintained the tool with no engineer. | [ADR 0001](docs/adr/0001-code-over-no-code.md) |
| Composio or another managed connector | It would hold the sign-ins for finance mailboxes. | [ADR 0002](docs/adr/0002-direct-google-apis-over-managed-connectors.md) |
| LangGraph | It structures the reasoning inside one model-driven task. It does not distribute work, limit rates or schedule. | [ADR 0003](docs/adr/0003-workflow-orchestrator-over-agent-framework.md) |
| Dagster, Temporal, Airflow | A poor fit for a variable number of emails, or too heavy for a tool that must run from one command. | [ADR 0003](docs/adr/0003-workflow-orchestrator-over-agent-framework.md) |
| A stronger Claude model for every document | It pays a premium on every document to help the few that need it. | [ADR 0008](docs/adr/0008-haiku-first-with-escalation.md) |
| Claude Haiku to classify | Equal accuracy to Jev at many times the cost. It remains the fallback. | [ADR 0009](docs/adr/0009-classifier-chosen-by-eval.md) |
| DeepEval or another evaluation library | Built around a model as judge, which is for outputs with no single right answer. Ours have one. | [ADR 0011](docs/adr/0011-exact-scoring-not-a-model-as-judge.md) |
| Letting the model write database queries | A wrong query still returns a confident number. | [ADR 0007](docs/adr/0007-fixed-queries-for-ask-your-invoices.md) |
| Signing in to vendor portals | Two-factor sign-in, bot detection, vendors' terms, and the risk of holding a file of administrator passwords. | [ADR 0012](docs/adr/0012-email-content-is-untrusted.md) |
| Review and vendor tabs in the Google Sheet | The tool rewrites the sheet on every run, so a person's edits would be lost or need merging. | [ADR 0006](docs/adr/0006-dashboard-is-the-only-action-surface.md) |
| A digest by email | A mailbox would need permission to send. | |
| Hosting on Google Cloud Run for the submission | It would break within seven days and show nothing in a review. | [ADR 0015](docs/adr/0015-described-not-hosted.md) |
| Recording the path of a held document's local copy | An archive gives one link, from Drive when there is an owner account, so the run would need every archive to return a second one. The SHA-256 of the PDF is known wherever the PDF is saved, and it also notices a local copy that has been changed or replaced. | |
| A planning map of decision tickets | The route was already clear after the first round of decisions. | |

## Still open

| Question | State |
|---|---|
| Tracing model calls in Langfuse | Planned last, and optional: with no keys the tool runs as before. |
| Harder cases in the golden dataset | Needed before the eval can separate the models on classification. |
| A mode that runs with no credentials | Set aside until the tool is complete. |
| An n8n layer on top | Set aside until the tool is complete. |
| An invoice uploaded before it also arrives as an attachment | The attachment is known by its bytes and the upload by its link, so the attachment is filed and reported as a second charge. The reverse order is recognised. |
| A held upload dated in another month | It is held under the month of its invoice date. Running the month the email arrived in before it is decided brings it back to that month's queue, where it cannot be approved with that date. |
