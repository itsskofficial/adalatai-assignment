# Scaling: where the design stops, and what comes next

The tool runs on one machine with SQLite (ADR 0017). This document says where that design stops being enough, in what order, and what the architecture becomes then. None of it is built. It is here so the path is known before it is needed.

## What the tool handles today

| | Today | Measured or estimated |
|---|---|---|
| Source accounts | 3 | |
| Billing documents a month | About 40 | |
| Time for a run | About 2 minutes | Measured, over three real mailboxes |
| Model cost of a run | About $0.06 | Measured, as the run records it |
| Time to read one document | 2 to 6 seconds | Measured in the extraction spike and the eval |

## What breaks first

The order below is an estimate. None of these limits has been reached or tested. The first would be worth measuring with a load test over a few hundred generated emails before anything is changed.

| What breaks | Roughly when | What to do | Size of the change |
|---|---|---|---|
| A run takes too long, reading one email after another | Around a thousand documents a month | Read several emails at once. The run already can, with a limit on how many | A setting |
| Gmail and the models limit the rate of calls | The same range | Lower the limit on how many are read at once, and retry with backoff, which the run already does | A setting |
| Rendering and portal fetches take most of a run's time | Most invoices behind portals, with several emails read at once | The browser serves every examination from one thread, one page at a time, so the model calls go on at once but the browser work does not. Open a browser for each thread, or a pool of them | Small |
| The machine runs out of memory during a run | Several browsers at once, once there are several | A larger machine, or the runner on a machine of its own | Small |
| One machine must not be a single point of failure | When the dashboard must stay up | The architecture below | Large |
| Two machines must write to the ledger | When the runner and the app are apart | Postgres | Large |
| A second company uses the tool | The first customer who is not us | Workspaces: every row belongs to one | Large |

The first three are settings and machine sizes. The last three are the change of architecture this document is about.

## The architecture to move to

```mermaid
flowchart TB
    browser["Browser<br/>the screens"]

    subgraph gcp["Google Cloud"]
        service["Cloud Run service<br/>app: API and built front end"]
        job["Cloud Run job<br/>runner: one collection, then exits"]
        scheduler["Cloud Scheduler<br/>starts the job on the chosen day"]
        sql[("Cloud SQL, Postgres<br/>ledger, settings, runs")]
        storage[("Cloud Storage<br/>PDFs")]
        secrets["Secret Manager<br/>keys, OAuth client, stored sign-ins"]
        registry["Artifact Registry<br/>one image for both"]
        logs["Cloud Logging and an alert<br/>when a job fails"]
    end

    outside["Gmail, read-only<br/>Claude and Jev<br/>Drive and Sheets<br/>Slack"]

    browser -->|HTTPS| service
    service -->|"run again"| job
    service -->|"the day changed"| scheduler
    scheduler -->|starts| job
    service --> sql
    job --> sql
    job --> storage
    service --> secrets
    job --> secrets
    job --> outside
    registry -.-> service
    registry -.-> job
    job -.-> logs
```

| Need | Service | Why this one |
|---|---|---|
| Dashboard and API | Cloud Run service | Adds instances under load and costs nothing while idle. Gives an HTTPS address |
| A collection | Cloud Run job | Runs for minutes and exits. Several can run at once, one for each source account |
| When to run | Cloud Scheduler | Starts the job on the day chosen in the dashboard, which the app writes to it |
| Ledger | Cloud SQL, Postgres | The app and the job are separate machines and cannot share a file |
| PDFs | Cloud Storage | Durable files, reached from any machine. Drive remains the copy finance sees |
| Secrets and sign-ins | Secret Manager | Nothing secret in the image or on a disk |
| Deploying | Artifact Registry and GitHub Actions | One image, deployed to both, with no stored keys |

## What changes in the code

Four things differ between one machine and this architecture. Each is behind an interface today, or is opened in one place.

| Interface | One machine | Cloud Run |
|---|---|---|
| Ledger | SQLite file | Postgres |
| PDF archive | Folder on the disk | Cloud Storage |
| Stored sign-ins | Files on the disk | Secret Manager |
| Starting a run | The app asks the runner | The app starts the Cloud Run job |
| Schedule | A timer in the runner | Cloud Scheduler, written by the app |

### The ledger on Postgres

This is the largest piece.

| Work | What it involves |
|---|---|
| One database layer | Every connection to the ledger is already opened in one place. That place chooses the database from a setting |
| Queries | Most are plain SQL that both databases accept. The few written for SQLite are rewritten in a form both accept |
| Schema changes | The hand-written additions of columns are replaced by a migration tool |
| Types | Dates and amounts are text today. Postgres has types for both |
| Tests on both | The tests stay on SQLite, which needs no server and gives each test a database of its own. A second job in CI runs the ledger and dashboard tests against a real Postgres |

SQLite stays for the tests and for the command line, where needing nothing installed is worth more than matching production.

The estimate is half a day to a day of work for the port, and as much again for the deployment. It is an estimate: the unknown is how many tests pass on SQLite and fail on Postgres.

### Reading several source accounts at once

On one machine a run reads the source accounts in turn. As jobs, each source account can be a job of its own, started together. The run already records each source account's outcome apart from the others, and running one account again leaves the others as they were.

One rule of the run does not survive that split by itself. An email sent to several mailboxes is one document, read once: the copies are examined one after the other in one process, so the second finds in the ledger what the first collected and files nothing twice. Jobs started apart cannot see each other's work while it is under way, so two of them would read one document at once, each paying for the model call, and each filing it. Before reading, a job would have to claim the document in the ledger, by inserting its content hash so that only one insert wins, and the other job waits for the winner's record or moves on. That is a small change to the ledger and to the examination, and it needs Postgres, since only a shared database can hold a claim that two machines contend for. Until then, one job reads every source account in turn, as today.

### Several companies

Every table gains the workspace its rows belong to, every query is limited to one, and signing in places a person in theirs. This is the change that touches the most code and is the hardest to add late. It is not started, and nothing in the present design prevents it.

## When an orchestrator returns

Prefect was removed because nothing was left for it to manage (ADR 0017). The architecture above does not bring it back: it is still one run at a time, started by a scheduler, and everything in the table of ADR 0017 still holds.

An orchestrator earns its place at the stage after that, when the work is no longer one run but many units spread over many machines. For this tool that is several companies, each with many source accounts.

| What appears | Why the run cannot solve it alone |
|---|---|
| Hundreds of source accounts read at once | One process cannot, and jobs started apart know nothing of each other |
| Limits on the rate of calls, shared by every worker | Each worker counts only its own calls, so together they go over the limit of Gmail or of a model |
| One source account failing among hundreds | It must be tried again alone, on another machine, without the rest being done again |
| Steps that wait for each other | Gaps can be worked out for a company only when every one of its source accounts has been read |
| Seeing hundreds of runs at once | A record of each run is no longer enough; the whole must be seen |

These are the four reasons of ADR 0003, at the volume where they are true.

Three options would be weighed then, with the numbers of the day.

| Option | For | Against |
|---|---|---|
| Prefect | Plain Python. Runs on a developer's machine as it runs in production. Shows every run. Has limits on concurrency shared between workers | Its control plane is another company holding data about our runs, or a server of our own to keep |
| Cloud Tasks with Cloud Workflows | Part of Google Cloud, so no new company. A limit on rate and a retry for each task are built in | Workflows are written in YAML, and are hard to run on a developer's machine |
| Temporal | The strongest promises for work that runs long | The most to operate |

Prefect is the likely choice, for running the same on a developer's machine and because the run is already made of plain functions that an orchestrator can wrap. The work done under ADR 0003 showed that: core logic never imported it, and removing it touched one module. Bringing one back would be the same size of change.

## What it costs

These are estimates from published prices, for the volume of today. They should be checked against the pricing calculator before a decision rests on them.

| | One machine | Cloud Run |
|---|---|---|
| Compute | About $15 a month, always on | Near nothing at this use |
| Disk or database | About $2 | About $10 to $12 for the smallest Cloud SQL instance |
| Address, secrets, storage | About $4 | Under $1 |
| Total a month | About $20 | About $12 |
| Models, for each monthly run | $0.06 | $0.06 |

The two are within ten dollars of each other. The choice between them was never about money: it is about how much there is to build, to explain and to keep working, against a load that one machine handles with room to spare.

## What was decided against, and would be looked at again

| Option | Why not now | When to look again |
|---|---|---|
| Prefect, or another orchestrator | Nothing left for it to manage (ADR 0017) | Many units of work over many machines. See "When an orchestrator returns" above |
| Tracing model calls in a hosted service | The ledger records each run's cost and each document's history. It was built behind an interface and left out | Prompts changed daily by several people |
| A queue between the app and the runner | One run a month needs no queue | Runs requested faster than they finish |
