---
status: accepted
---

# Source accounts and people are managed in the dashboard; the setting is the way back in

Two lists decide what the tool can reach and who can reach the tool. They are separate, and both are managed in the dashboard.

| | Source accounts | People |
|---|---|---|
| What it is | Mailboxes the tool reads | People who may sign in to the dashboard |
| Example | `billing@company.com` | `priya@company.com` |
| Managed on | Source accounts screen | People screen |

## Source accounts

A person connects a mailbox from the dashboard and approves read-only access with Google. The tool checks that the address which signed in is the address being connected, and stores nothing if it is not. The command line can also sign an account in, for a machine with no dashboard, and both store the sign-in in the same place.

## People

Signing in with Google proves who someone is. It does not decide whether they are let in; the list of people does. There are two roles: an administrator manages the list, and a member does everything else.

The addresses in the `INVOICE_COLLECTOR_ALLOWLIST` setting are administrators who can always sign in and cannot be removed or changed from the dashboard.

## Considered Options

- **A setting for both lists, edited by an engineer**: rejected. Adding a mailbox or a colleague would need an engineer each time.
- **Both lists only in the dashboard**: rejected. A mistake on the People screen could leave nobody able to sign in, with no way to repair it from inside.
- **Both in the dashboard, with the setting as a floor**: chosen.

## Consequences

The tool refuses to start if nobody could sign in.

A person removed from the list is refused on their next request, without waiting for their session to end.

Every change to either list is recorded with who made it and when.

While the OAuth app is in testing, Google expires a sign-in after seven days. The Source accounts screen marks a sign-in that is about to expire and renews it with one button.
