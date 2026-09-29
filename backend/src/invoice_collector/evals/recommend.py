"""The rule of ADR 0009, applied to a scorecard.

For each job, Jev becomes the default only if it matches Claude Haiku on accuracy and is better
calibrated. Otherwise Claude Haiku stays the default. The ADR itself is edited by a person.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from invoice_collector.evals.scoring import ACCURACY, CandidateResult

HAIKU = "claude-haiku"
JEV = "jev"
UNDECIDED = "undecided"
# How far below Claude Haiku's accuracy Jev may be and still "match" it.
ACCURACY_MATCH_TOLERANCE = 0.0


@dataclass(frozen=True)
class Recommendation:
    job: str
    choice: str
    """The candidate the rule selects, or "undecided" when it cannot be applied."""
    reason: str


def _share(result: CandidateResult) -> float:
    return result.metrics[ACCURACY].share or 0.0


def adr_0009(job: str, results: Sequence[CandidateResult]) -> Recommendation:
    by_name = {r.name: r for r in results if r.job == job}
    haiku, jev = by_name.get(HAIKU), by_name.get(JEV)
    if haiku is None or not haiku.ran:
        why = "was not scored" if haiku is None else f"was {haiku.status}"
        return Recommendation(
            job,
            UNDECIDED,
            f"Claude Haiku {why}, so there is nothing to compare Jev with. "
            "Claude Haiku stays the default until both are scored.",
        )
    if jev is None or not jev.ran:
        why = "was not scored" if jev is None else f"was {jev.status}"
        return Recommendation(job, HAIKU, f"Jev {why}, so Claude Haiku stays the default.")

    haiku_accuracy, jev_accuracy = _share(haiku), _share(jev)
    accuracies = f"accuracy {jev_accuracy:.3f} against Claude Haiku's {haiku_accuracy:.3f}"
    if jev_accuracy < haiku_accuracy - ACCURACY_MATCH_TOLERANCE:
        return Recommendation(job, HAIKU, f"Jev does not match Claude Haiku: {accuracies}.")

    haiku_error, jev_error = haiku.calibration_error, jev.calibration_error
    if jev_error is None:
        return Recommendation(
            job, HAIKU, f"Jev matches on {accuracies}, but its calibration could not be measured."
        )
    if haiku_error is not None and jev_error >= haiku_error:
        return Recommendation(
            job,
            HAIKU,
            f"Jev matches on {accuracies}, but is not better calibrated: calibration error "
            f"{jev_error:.3f} against Claude Haiku's {haiku_error:.3f}.",
        )
    against = "" if haiku_error is None else f" against Claude Haiku's {haiku_error:.3f}"
    return Recommendation(
        job,
        JEV,
        f"Jev matches on {accuracies}, and is better calibrated: calibration error "
        f"{jev_error:.3f}{against}.",
    )
