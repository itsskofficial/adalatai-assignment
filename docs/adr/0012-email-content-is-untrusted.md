---
status: accepted
---

# What an email contains, and where its links lead, is not trusted

Anyone can send an email to a mailbox the tool reads. The tool renders the body of that email in a browser and opens links found in it, from a machine inside the company's network. Both are therefore treated as chosen by an attacker.

| Risk | What the tool does |
|---|---|
| A script in an email body runs in the browser | Email bodies are rendered with scripts off |
| An email body loads something from the network, such as a tracking image | Email bodies are rendered with no network access; only images carried inside the email are shown |
| A link points at a machine inside the network | Only secure links to public addresses are opened |
| A link redirects to a machine inside the network | Each redirect is checked before it is followed, for the page and for everything the page requests |
| A name gives a public address when checked and a private one when opened | Every connection goes through a proxy inside the tool, which looks the name up itself and connects to the address it checked |
| An unsubscribe link is opened and acted on | A link is followed only if its address or text mentions an invoice, receipt, billing or statement |
| A link with a single-use token is spent by a check | A portal page is requested once, and what is rendered is the response that was checked |
| Text from an email runs as a formula when the summary is opened | Text cells that begin with a formula character are written as text |
| The tool signs in to a vendor's portal | It never types into a page or submits a form |

## Considered Options

- **Trust mail from known vendors**: rejected. A sender's address is easily forged.
- **Check a link once, then let the browser open it**: rejected after review. The name is looked up a second time when the browser connects, and the answer can differ.
- **Treat everything as hostile, and check at the moment of connecting**: chosen.

## Consequences

Logos loaded from the network do not appear in a PDF rendered from an email body. The amounts, dates and text do.

Sample portal pages served from the same machine are refused unless the run is started with `--allow-local-portals`, which is for demonstration only and must never be used with real mail.

Most of this came out of code review of the change that added email bodies and portal links.
