---
status: accepted
---

# The dashboard is the only place people take actions; the sheet is a read-only report

The assignment asks for a summary sheet as an output, and offers CSV as an equal alternative, so it is a report. Human review and vendor list upkeep are our additions, and they need somewhere to happen. We put every action in one dashboard and keep the sheet, the CSV and the Drive folder as outputs the tool regenerates on each run. The ledger is the source of truth; the sheet and the dashboard are both views of it.

Actions in the dashboard: reviewing low-confidence invoices, accepting or ignoring suggested vendors, editing the expected vendor list, uploading an invoice downloaded by hand from a portal, and re-running a month or a failed account.

## Considered Options

- **Review and vendor tabs in the Google Sheet**: rejected. It needs no extra interface, but the tool would have to merge human edits into a sheet it regenerates, and actions would be split between the sheet and wherever uploads happen.
- **Dashboard for all actions**: chosen.

## Consequences

The dashboard is a required part of the tool, not an extra. It is built with FastAPI and React.

An invoice awaiting review is not written to the main summary until a person approves it.

A file in the repo seeds the expected vendor list on first run; after that the ledger holds it.
