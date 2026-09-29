---
status: accepted
---

# "Ask your invoices" chooses from fixed queries; the model never writes its own

The dashboard lets a person ask questions such as "how much did we spend on AWS last quarter?". The model picks one of a fixed set of queries we wrote and fills in its parameters; it does not write database queries itself. A model-written query that is subtly wrong still returns a confident number, and a wrong spend figure is worse for finance than a question the tool declines to answer.

## Considered Options

- **Model writes a read-only query per question**: rejected. It can answer anything, but its answers cannot be tested in advance.
- **Fixed set of queries**: chosen.

## Consequences

Every answer shows the billing documents behind the number.

A question outside the fixed set gets a plain "I can't answer that yet", and is logged so the set can grow from real demand.
