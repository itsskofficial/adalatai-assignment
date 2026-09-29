---
status: proposed
---

# The classifier is chosen by the eval: Claude Haiku 4.5 against Jev

Classification decides whether an email is a billing document, a billing signal or neither. The answer comes from a fixed list, which is the job a classification model is built for. Jev, released by Typesafe AI in September 2026, returns a choice with a probability that is trained to match how often it is right. We build the classifier on both Jev and Claude Haiku 4.5, run both through the offline eval, and keep as the default whichever scores better. Extraction stays on Claude either way, because Jev reads text only and cannot return free-form values such as a vendor name or an amount.

## Why consider a second model at all

In the extraction spike, Haiku rated its own confidence as high on every document. A rating that never varies cannot decide what goes to review, which is why ADR 0004 relies on cross-checks instead. A calibrated probability would make "hold for review below 90%" a threshold with meaning.

Cost is not the reason at today's volume: classification costs about $0.14 a month on Haiku. It would matter at 150,000 emails a month, where the reported prices give about $142 on Haiku and about $5 on Jev.

## How the eval decides

Both classifiers are scored on the golden dataset for:

- precision and recall on "is this a billing document"
- accuracy on document type
- calibration: among answers given at a stated probability, the share that were right
- time per call

Jev becomes the default only if it matches Haiku on precision and recall and is better calibrated. This ADR is accepted, with the result recorded here, once the eval has run.

## Considered Options

- **Haiku only**: simplest, one vendor. Leaves the confidence problem to cross-checks alone.
- **Jev only**: rejected. It was released two weeks before this decision, its published figures are the vendor's own, and access is by waitlist. The tool must work without it.
- **Both, chosen by eval**: chosen.

## Consequences

The classifier is a module with two implementations, selected by configuration. With no Jev key present the tool uses Haiku, so anyone can run it with a single API key.

Email text is sent to a second company when Jev is selected. Its data retention terms must be read and stated in the README before Jev is made the default.

Jev may also serve as a cross-check on extraction, by asking a yes-or-no question such as "is the total 652.50?" against the text of the document. This is tried only if the classifier comparison favours Jev.

Figures for Jev in this document come from third-party write-ups and are unverified until the eval runs.
