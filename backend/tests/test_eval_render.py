"""Writing the scorecard again from the results it stores, without calling any model."""

import json
from pathlib import Path
from typing import Any

from invoice_collector.evals.cli import (
    DEFAULT_HARD,
    DEFAULT_OUT,
    DEFAULT_QUESTIONS,
    DEFAULT_SAMPLES,
    main,
)
from invoice_collector.evals.golden import HARD, STANDARD, load_expected_vendors
from invoice_collector.evals.questions import load_questions
from invoice_collector.evals.recommend import RULE
from invoice_collector.evals.scorecard import from_json, to_json, to_markdown

STORED = DEFAULT_OUT / "scorecard.json"


def stored() -> dict[str, Any]:
    return json.loads(STORED.read_text("utf-8"))


def read_back(data: dict[str, Any]) -> Any:
    expected = {
        STANDARD: load_expected_vendors(DEFAULT_SAMPLES),
        HARD: load_expected_vendors(DEFAULT_HARD),
    }
    return from_json(data, expected, load_questions(DEFAULT_QUESTIONS))


def test_stored_results_read_back_give_the_same_scorecard() -> None:
    data = stored()

    assert to_json(read_back(data)) == data


def test_the_committed_scorecard_is_what_its_stored_results_render(tmp_path: Path) -> None:
    code = main(["render", "--out", str(tmp_path)])

    assert code == 0
    assert (tmp_path / "scorecard.md").read_text("utf-8") == (
        DEFAULT_OUT / "scorecard.md"
    ).read_text("utf-8")
    assert json.loads((tmp_path / "scorecard.json").read_text("utf-8")) == stored()


def test_the_scorecard_states_the_rule_it_applies() -> None:
    markdown = to_markdown(read_back(stored()))

    assert markdown.count(RULE) == 2  # once for each golden set


def test_rendering_changes_no_score(tmp_path: Path) -> None:
    main(["render", "--out", str(tmp_path)])

    rendered = json.loads((tmp_path / "scorecard.json").read_text("utf-8"))
    assert rendered["scores"] == stored()["scores"]
    assert rendered["paid_this_run_usd"] == stored()["paid_this_run_usd"]
