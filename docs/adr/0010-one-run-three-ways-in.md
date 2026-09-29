---
status: accepted
---

# One run, reached three ways: the schedule, the dashboard and the command line

Collecting a month is one function. It is started by a schedule, by a person in the dashboard, or by an engineer at the command line, and it behaves the same whichever started it. The command line is kept because the people who build and operate the tool need it; the people who use its output never see it.

| Way in | Who | For what |
|---|---|---|
| Schedule | Nobody; it runs itself | The monthly collection |
| Dashboard | Finance | Review, vendors, source accounts, re-running a month |
| Command line | Engineers | Development, seeding sample mail, the eval, CI, recovering a month when the dashboard is the problem |

## Considered Options

- **Dashboard only**: rejected. The schedule, CI and the eval have no person to click anything, and a pipeline that can only be started from a browser cannot be tested without one.
- **Command line only**: rejected. Finance should not need a terminal to connect a mailbox or confirm an invoice.
- **Both, over one function**: chosen.

## Consequences

Anything a person in finance must do has to be possible in the dashboard. Connecting a source account was at first possible only from the command line, which was a gap; it was closed by the Source accounts screen (ADR 0014).

The tool is deployed once and used by the whole finance team through one address. Secrets are set by whoever deploys it and are never handled by finance.
