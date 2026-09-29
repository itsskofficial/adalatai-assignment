---
status: accepted
---

# Tracing of model calls in Langfuse was built and left out

Every call to a model was to be traced in Langfuse, a hosted service for observing model calls. It was built behind an interface, tested, and then not merged. The branch `feat/langfuse-tracing` holds it.

## Why it was left out

By the time tracing was built, the tool answered the same questions itself.

| What tracing gives | What the tool already has |
|---|---|
| What each model call cost | Each run records its calls, tokens and cost for every model, shown on the Runs screen and in the digest |
| What happened to a billing document | The audit trail shows every step, with the model that read it and the checks that ran |
| How accuracy moves over time | The offline eval's scorecard, and a baseline that fails the build when accuracy falls |
| What people corrected | Each correction is kept with who made it, when, and the value before and after |

What tracing adds beyond these is a screen for looking at single model calls with their timings, and a hosted view for comparing eval runs. Those pay for themselves when prompts are changed daily by several people. With one run a month, they do not.

It also has costs of its own.

- **A third party would receive data about finance documents.** Only measurements by default, not the email or the PDF, but a third party all the same.
- **A large dependency**, with its own ways of failing.
- **One more thing to run and to explain.**

## Considered Options

- **Merge it, switched off unless keys are present**: rejected. Code that is not used still has to be kept working, and every model adapter would carry it.
- **Delete the work**: rejected. It is sound, and it shows what adding tracing would take.
- **Leave it on its branch, unmerged**: chosen.

## Consequences

The model adapters report what each call used to a meter, and nothing else.

If tracing is wanted later, the branch shows the design: a `Tracer` interface with one implementation, a call's tokens read once and given to both the meter and the trace, and only measurements sent unless a setting says otherwise. It would be brought up to date with the code of the day, not merged as it is.

What would be sent to a third party, and what never would, was decided on that branch. That decision is to be made again, in its own record, if tracing is added.
