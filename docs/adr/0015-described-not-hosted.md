---
status: superseded by 0017
---

# The production deployment is described, not hosted

The tool is packaged so that it can be deployed, and the README describes how. It is not hosted anywhere for the submission.

## Why

- **A hosted copy would break on its own.** While the OAuth app is in testing, Google expires every sign-in after seven days. A scheduled job in the cloud would stop reading mail a week after the accounts were last connected. A demonstration that is broken on the day it is looked at is worse than none.
- **A scheduled job shows nothing.** It runs once a month in the background.
- **What shows that a tool can be deployed is how it is built**: configuration through the environment, secrets kept out of the code and the image, storage behind interfaces, and steps that can be repeated safely.

## Considered Options

- **Host the dashboard and the schedule on Google Cloud Run**: rejected for the submission, for the reasons above. It remains the intended production path.
- **Describe the deployment and demonstrate the tool running locally**: chosen.

## The production path

One deployment serves the whole finance team.

| Part | In production |
|---|---|
| Dashboard and API | One container |
| Monthly schedule | The same container, started by a scheduler |
| Ledger | Postgres, in place of the SQLite file |
| Secrets | A secret manager |
| Mailbox access | Workspace domain-wide delegation, in place of a sign-in per account |
| Alerts | The Slack digest, which also reports a failed run |

## Consequences

The review is given by running the tool live against the three test mailboxes, with a recording as a fallback.

The README states the seven-day limit plainly.
