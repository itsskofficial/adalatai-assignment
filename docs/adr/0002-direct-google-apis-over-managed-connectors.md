---
status: accepted
---

# Direct Google APIs over a managed connector

Mailbox access goes through Google's own APIs using our own OAuth app, registered in a Google Cloud project, with read-only Gmail scope. Managed connector services such as Composio remove the OAuth setup, but they hold the mailbox tokens on their side, and these are finance mailboxes. Keeping tokens on our own machine is worth the one-off setup cost.

## Considered Options

- **Composio via MCP**: rejected. MCP exists so an LLM agent can choose tools at runtime; this is a deterministic monthly batch that must run the same way every time.
- **Composio via its SDK**: rejected as the default for the token custody reason above, and because anyone running the tool would need an account with a third party.
- **Direct Google APIs**: chosen.

## Consequences

Mail access sits behind a `MailSource` interface, so a managed connector can be added later as another adapter without touching the pipeline.

An OAuth app in Testing mode issues refresh tokens that expire after 7 days, so accounts need reconnecting weekly until the app is published or replaced by Workspace domain-wide delegation.
