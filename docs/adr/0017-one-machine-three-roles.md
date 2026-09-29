---
status: accepted
supersedes: 0003, 0015
---

# One machine, three roles: front end, app and runner, with SQLite

The tool is deployed on one virtual machine with Docker Compose. Three roles share one disk, which holds the ledger, the PDFs and the stored sign-ins. The same Compose file runs on a developer's or a reviewer's machine.

| Role | What it does | What it never does |
|---|---|---|
| Front end | The screens, in the browser | Holds a secret |
| App | Serves the API and the built front end | Collects |
| Runner | Collects, on the schedule or when the app asks | Serves a page |

The app asks the runner directly, over the private network between them. Nothing polls. The schedule is a timer in the runner, set from the day chosen on the Settings screen.

This supersedes two earlier decisions. ADR 0003 chose Prefect to orchestrate runs. ADR 0015 chose to describe the deployment and not host it.

## Why one machine

The workload is three source accounts and one run a month, about forty billing documents. One small machine is two orders of magnitude above that.

- **Local and production are the same.** A reviewer runs the Compose file that production runs. Nothing differs between them but the settings.
- **The ledger stays a file.** SQLite needs no server, no password and no network port, and it is backed up by copying it. It allows one machine to write, which is what we have.
- **A process runs until it is done.** A run takes minutes. On a machine of our own nothing stops it midway.

## Why Prefect was removed

ADR 0003 gave four reasons for an orchestrator: scheduling, run visibility, retries for one email, and rate limiting. By the time the tool was complete, each had another answer.

| Reason in ADR 0003 | What does it now |
|---|---|
| Scheduling | The runner's timer, set in the dashboard |
| Run visibility | The ledger records each run; the Runs screen and the digest show it |
| Retries for one email | The run retries with backoff (ADR 0005) |
| Rate limiting | Not needed at this volume; a limit on how many emails are read at once |

A schedule chosen in the dashboard has to live in our own settings. Held in Prefect as well, it would be in two places that must be kept in step. With nothing left for it to manage, Prefect was a dependency without a job.

This is a decision about the workload of today, not about orchestrators. ADR 0003's reasoning becomes true when the work stops being one run and becomes many, spread over many machines. `docs/research/scaling.md` says when that is, and what would be weighed then.

## Why it is now hosted

ADR 0015 held back from hosting because Google ends each sign-in after seven days while the OAuth app is in testing, and a hosted copy would stop reading mail on its own. That limit remains. It is now met in the dashboard: the Source accounts screen marks a sign-in that is about to end and renews it with one button, and a run that cannot read an account says so in the digest. A reviewer can also run the tool on their own machine against their own test mailboxes, so the hosted copy is no longer the only way to see it work.

## Considered Options

- **Cloud Run for the app and the runner, with Postgres**: rejected for now. The app and the runner would be separate machines with no shared disk, so the ledger would have to move to a database on the network. That is a real piece of work that buys nothing at this volume. Its cost is about the same as one machine. It is the path when a second machine must write to the ledger, and `docs/research/scaling.md` describes it.
- **Vercel for the front end and Supabase for the database**: rejected. The run needs a headless browser and minutes of time, which a serverless function does not give, so a third place would still be needed to run it. Two more companies would hold finance data.
- **One process for everything**: rejected. The app would collect on a thread of its own. It works on one machine, but it ties how long a run may take to the life of the web service, and the two could not be moved apart later without a rewrite.
- **Prefect holding the schedule and starting runs**: rejected, for the reasons above.
- **The runner looking in the ledger every few minutes for work**: rejected. It works and recovers by itself after an outage, but a direct request and a timer are simpler to explain, and the runner checks once for a missed run when it starts.
- **One machine, three roles, SQLite**: chosen.

## Consequences

The machine is ours to keep: it is patched, backed up by scheduled snapshots of its disk, and watched by an uptime check.

Secrets are set by whoever deploys and are never settings of the dashboard. The day of the schedule and the Drive folder are settings of the dashboard and never secrets.

The runner listens on the private network only, and the app proves itself to it with a shared secret, so reaching the runner's port is not enough to start a run.

One machine is one point of failure. If it stops, the dashboard is unreachable and a scheduled run waits until it is back. For a run a month, that is accepted.

When the ledger must be written from a second machine, it moves to Postgres. Every connection to it is opened in one place for that reason.
