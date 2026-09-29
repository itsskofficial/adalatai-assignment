---
status: accepted
---

# Jev classifies emails; rules match vendors, with Jev for what rules cannot decide

Two jobs in the pipeline choose from a known list: classifying an email, and matching a billing document to an expected vendor. Both were built on more than one model and scored by the offline eval on 2026-09-29. Jev is the default classifier, with Claude Haiku 4.5 and then rules behind it. Vendors are matched by rules first, and Jev is asked only when the rules cannot decide. Extraction stays on Claude, because Jev reads text only and cannot return free-form values such as an amount.

## What the eval found

Classification, 93 emails:

| | Jev | Claude Haiku 4.5 | Rules |
|---|---|---|---|
| Accuracy | 100% | 100% | 98.9% |
| Cost of 93 answers | $0.0022 | $0.0615 | none |
| Time per call, median | 0.37 s | 0.97 s | under 0.1 s |

Vendor matching, 58 billing documents, two of them from vendors not on the expected list:

| | Jev | Claude Haiku 4.5 | Rules |
|---|---|---|---|
| Accuracy | 100% | 100% | 100% |
| Cost of 58 answers | under $0.01 | about $0.03 | none |

## Why Jev for classification

Jev and Claude Haiku tied on accuracy. Jev cost about one twenty-eighth as much and answered in about a third of the time. At equal accuracy, the cheaper and faster model is the default.

The rules were not chosen although they cost nothing, because they made the one mistake that matters here: they took a usage alert that mentions an invoice to be an invoice. A classifier has to judge what an email is, and that is where a model earns its cost.

## Why rules first for vendor matching

All three were right every time, so the one that costs nothing, needs no network and gives the same answer on every run is the default. The rules decline to answer when a document names no expected vendor, or more than one. Jev is asked then, and only then.

## What this decision does not rest on

Calibration. ADR 0004 hoped that a calibrated probability would make "hold for review below 90%" a threshold with meaning. The eval could not show that. Neither model made a classification mistake, so there were no wrong answers to be confident or unconfident about. Jev stated a probability near 1.00 on every answer and Claude Haiku said "high" on every answer. Holding for review therefore still rests on the checks of ADR 0004 and not on any model's stated confidence.

## Considered Options

- **Claude Haiku for both**: rejected. Equal accuracy at many times the cost.
- **Jev for both**: rejected for vendor matching, where rules do as well for nothing.
- **Rules for both**: rejected for classification, where rules were wrong on a case that matters.
- **Jev to classify; rules then Jev to match**: chosen.

## Consequences

With no Jev key the tool classifies with Claude Haiku, and with no key at all it uses rules, so it runs with whatever keys are present.

Email text is sent to Typesafe AI when Jev is in use. Its terms say inputs are not used for training, give no fixed retention period, and offer zero retention to enterprise customers only. The README states this.

The golden dataset is too easy to separate the models. It needs cases that at least one of them gets wrong: forwarded mail, ambiguous subjects, vendors with similar names. This decision is to be revisited when it has them.

Jev was released two weeks before this decision. If it becomes unavailable, the fallback to Claude Haiku keeps the tool working with no change.

## Revisited on the hard set

The hard golden set was run on 2026-09-29: 28 emails and 23 billing documents made of what real invoices get wrong.

| | Jev | Claude Haiku 4.5 | Claude Sonnet 5.5 | Rules |
|---|---|---|---|---|
| Classification, 28 emails | 92.9% | 92.9% | 92.9% | 78.6% |
| Vendor matching, 23 documents | 100% | 95.7% | not run | 91.3% |

The decision stands, for these reasons:

- **Classification is still a tie**, and Jev is still the cheapest and fastest. Every model took a paid invoice with nothing left to pay for a receipt.
- **Jev is the better matcher for what rules cannot decide.** Rules and Claude Haiku both matched a receipt from Loom, which names its parent Atlassian, to Atlassian. Jev did not.
- **Rules alone are not enough on hard documents**, which is why a model stands behind them for both jobs.

One thing changed. Jev took an invoice for something else when the email carried a line telling the reader to classify it as not a billing document. It stated a probability of 0.51 when it did. A probability that low is now treated as no answer, and the next classifier is asked, so a model in doubt is never the last word on whether an email is dropped. The history of a billing document names the classifier whose answer was used.

The figures above score each matcher alone. Since then the eval scores a model behind the rules, as a run uses it (ADR 0016): the rules decide what they can, and the model is asked the rest. The next live run gives figures on that footing.
