"""The rule of ADR 0009, applied to a scorecard.

For each job, accuracy decides first, and calibration only separates candidates of equal
accuracy: Jev becomes the default when it is more accurate than Claude Haiku, whatever its
calibration, or when it is as accurate and better calibrated. Otherwise Claude Haiku stays
the default. The ADR itself is edited by a person.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from invoice_collector.evals.scoring import ACCURACY, CandidateResult

HAIKU = "claude-haiku"
JEV = "jev"
UNDECIDED = "undecided"
# How far apart the two accuracies may be and still be equal. More than this above Claude
# Haiku's, Jev exceeds it; more than this below, Jev falls short of it.
ACCURACY_MATCH_TOLERANCE = 0.0

RULE = (
    "The rule: for each job, accuracy decides first, and calibration only separates candidates "
    "of equal accuracy. Jev becomes the default if it is more accurate than Claude Haiku, "
    "whatever its calibration, or if it is as accurate and better calibrated. Otherwise Claude "
    "Haiku stays the default."
)


@dataclass(frozen=True)
class Recommendation:
    job: str
    choice: str
    """The candidate the rule selects, or "undecided" when it cannot be applied."""
    reason: str


def _share(result: CandidateResult) -> float:
    return result.metrics[ACCURACY].share or 0.0


def _calibrations(jev_error: float | None, haiku_error: float | None) -> str:
    if jev_error is None:
        return "Jev's calibration could not be measured"
    against = "" if haiku_error is None else f" against Claude Haiku's {haiku_error:.3f}"
    return f"calibration error {jev_error:.3f}{against}"


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
    haiku_error, jev_error = haiku.calibration_error, jev.calibration_error
    accuracies = f"accuracy, {jev_accuracy:.3f} against {haiku_accuracy:.3f}"
    if jev_accuracy > haiku_accuracy + ACCURACY_MATCH_TOLERANCE:
        return Recommendation(
            job,
            JEV,
            f"Jev exceeds Claude Haiku on {accuracies}, and accuracy decides before calibration "
            f"({_calibrations(jev_error, haiku_error)}).",
        )
    if jev_accuracy < haiku_accuracy - ACCURACY_MATCH_TOLERANCE:
        return Recommendation(job, HAIKU, f"Jev falls short of Claude Haiku on {accuracies}.")

    matches = f"Jev matches Claude Haiku on {accuracies}"
    if jev_error is None:
        return Recommendation(job, HAIKU, f"{matches}, but its calibration could not be measured.")
    if haiku_error is not None and jev_error >= haiku_error:
        return Recommendation(
            job,
            HAIKU,
            f"{matches}, but is not better calibrated: {_calibrations(jev_error, haiku_error)}.",
        )
    caveat = ""
    if haiku_accuracy == jev_accuracy == 1.0:
        caveat = (
            " Neither made a mistake, so the difference in calibration only reflects Jev stating "
            "probabilities near 1.00 where Claude Haiku's label is read as a fixed probability. "
            "Hard cases that one of them gets wrong are needed before this means much."
        )
    return Recommendation(
        job,
        JEV,
        f"{matches}, and is better calibrated: {_calibrations(jev_error, haiku_error)}.{caveat}",
    )
