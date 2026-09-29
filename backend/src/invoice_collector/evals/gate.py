"""The regression gate: compares a scorecard's scores with the accepted baseline."""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from invoice_collector.evals.candidates import NO_KEY
from invoice_collector.evals.scoring import ALL_FIELDS_RIGHT, BILLING_RECALL

DEFAULT_TOLERANCE = 0.05
TOLERANCES: Mapping[str, float] = {ALL_FIELDS_RIGHT: 0.0, BILLING_RECALL: 0.0}
"""By metric name. A metric not named here may fall by DEFAULT_TOLERANCE."""

_EPSILON = 1e-9


@dataclass
class GateResult:
    falls: list[str] = field(default_factory=list[str])
    notes: list[str] = field(default_factory=list[str])

    @property
    def passed(self) -> bool:
        return not self.falls


def tolerance_for(metric: str, overrides: Mapping[str, float]) -> float:
    """An override for the full metric path wins, then one for its name, then the default."""
    name = metric.rsplit(".", 1)[-1]
    for table in (overrides, TOLERANCES):
        if metric in table:
            return table[metric]
        if name in table:
            return table[name]
    return DEFAULT_TOLERANCE


def check(
    scorecard: Mapping[str, Any],
    baseline: Mapping[str, Any],
    overrides: Mapping[str, float] | None = None,
) -> GateResult:
    overrides = overrides or {}
    current = cast(Mapping[str, float], scorecard.get("scores", {}))
    not_run = cast(Mapping[str, str], scorecard.get("not_run", {}))
    chosen = set(cast(list[str], scorecard.get("candidates", [])))
    accepted_scores = cast(Mapping[str, float], baseline.get("scores", {}))
    result = GateResult()
    for metric, accepted in sorted(accepted_scores.items()):
        now = current.get(metric)
        if now is None:
            candidate = metric.rsplit(".", 1)[0]
            status = not_run.get(candidate)
            if status == f"not run: {NO_KEY}":
                result.notes.append(f"{metric}: not measured ({status})")
            elif candidate not in chosen:
                result.notes.append(f"{metric}: not measured (not chosen for this run)")
            else:
                result.falls.append(
                    f"{metric}: not measured ({status or 'missing from the scorecard'})"
                )
            continue
        tolerance = tolerance_for(metric, overrides)
        if now < accepted - tolerance - _EPSILON:
            result.falls.append(
                f"{metric} fell from {accepted:.4f} to {now:.4f} (tolerance {tolerance:g})"
            )
    return result


def read_json(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(path.read_text("utf-8")))


def accept(scorecard: Mapping[str, Any], baseline_path: Path) -> None:
    """Writes the scorecard's scores as the new accepted baseline."""
    scores = cast(Mapping[str, float], scorecard.get("scores", {}))
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(
        json.dumps({"scores": dict(sorted(scores.items()))}, indent=2) + "\n", "utf-8"
    )
