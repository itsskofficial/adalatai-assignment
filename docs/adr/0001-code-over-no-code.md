---
status: accepted
---

# Code over a no-code workflow tool

The assignment accepts either code or a workflow export, and n8n would deliver the Gmail to Drive to Sheets wiring in about a day. We are writing code anyway, because the difficult part of this problem is getting financial data right, and that needs fixture-based tests, reviewable diffs, an audit trail and safe re-runs. Once extraction, duplicate detection and gap logic are involved, an n8n workflow becomes code pasted into Code nodes without the tooling that makes code safe to change.

## Considered Options

- **n8n (or Zapier, Make)**: rejected as the core. Built-in connectors and a visual flow that non-engineers can read, and n8n's queue mode means scale is not the objection. The objections are testing, change review, local HTML-to-PDF rendering and auditability.
- **Code**: chosen.

## Consequences

This assumes engineering owns the tool. If finance or ops were to maintain it with no engineer available, n8n would be the better choice and this decision should be revisited.

Because the tool runs from a single command, a no-code tool can still sit on top of it as a thin trigger and notification layer.
