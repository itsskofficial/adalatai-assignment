"""The questions eval for Ask your invoices: exact scoring of the chosen query and parameters.

No model is called. Candidates are fakes that return prepared choices, or the real Answerer
pointed at a local server replaying a hand-written tool-use response.
"""

import json
import os
from collections import Counter
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from conftest import ReplayClient

from invoice_collector.api.questions import (
    CANNOT_ANSWER_TOOL,
    FIXED_QUERIES,
    Answerer,
    Choice,
    Ledgered,
    Parameters,
)
from invoice_collector.domain import CollectionMonth
from invoice_collector.evals.candidates import (
    NO_KEY,
    Candidate,
    QueryChooser,
    asker_candidate,
)
from invoice_collector.evals.cli import main
from invoice_collector.evals.evaluate import Settings, run_questions
from invoice_collector.evals.questions import (
    ANSWERABLE,
    KIND,
    QUERY,
    UNANSWERABLE,
    Asked,
    QuestionCase,
    QuestionSet,
    ask_chooser,
    load_questions,
)
from invoice_collector.evals.runner import AnswerCache
from invoice_collector.evals.scorecard import QuestionsCard, Report, to_json, to_markdown
from invoice_collector.evals.scoring import ACCURACY, Rate
from invoice_collector.fixed_queries import Period
from invoice_collector.renderer import FakeRenderer

BACKEND = Path(__file__).resolve().parents[1]
QUESTIONS = BACKEND / "evals" / "questions.json"
TOOL_USE: dict[str, Any] = json.loads(
    (Path(__file__).parent / "recorded/claude_question_tool_use.json").read_text("utf-8")
)
AUGUST = Period(CollectionMonth(2026, 8), CollectionMonth(2026, 8))


def choosing(name: str, parameters: dict[str, Any]) -> dict[str, Any]:
    block = {**TOOL_USE["content"][0], "name": name, "input": parameters}
    return {**TOOL_USE, "content": [block]}


def question(
    id: str, text: str, query: str, parameters: dict[str, str | int | None], *kinds: str
) -> QuestionCase:
    return QuestionCase(id, text, tuple(kinds), query, parameters)


AWS_AUGUST: dict[str, str | int | None] = {
    "vendor": "AWS",
    "source_account": None,
    "from_month": "2026-08",
    "to_month": "2026-08",
}


def small_set(*cases: QuestionCase) -> QuestionSet:
    return QuestionSet(
        today=date(2026, 9, 29),
        vendors=("AWS", "Slack"),
        source_accounts=("ops@example.test",),
        months=("2026-07", "2026-08"),
        cases=cases,
    )


class FakeChooser:
    """Returns a prepared choice for each question, and fails on the rest."""

    def __init__(self, choices: dict[str, Choice]) -> None:
        self._choices = choices

    def choose(self, question: str, ledger: Ledgered) -> Choice:
        try:
            return self._choices[question]
        except KeyError:
            raise RuntimeError("no prepared choice") from None


def total_spend(vendor: str | None = "AWS", period: Period | None = AUGUST) -> Choice:
    return Choice(FIXED_QUERIES["total_spend"], Parameters(vendor=vendor, period=period))


def declined(reason: str = "Forecasts are not among the fixed queries.") -> Choice:
    return Choice(None, None, reason)


def fake(chooser: QueryChooser) -> Candidate[QueryChooser]:
    return Candidate("fake", "claude-haiku-4-5", "v1", "anthropic", chooser)


SETTINGS = Settings(cache=None)


# --- Scoring -------------------------------------------------------------------------------------


def test_a_choice_is_right_only_when_query_and_parameters_are_exactly_right() -> None:
    cases = small_set(
        question("right", "AWS in August?", "total_spend", AWS_AUGUST, "plain"),
        question("wrong-month", "AWS last month?", "total_spend", AWS_AUGUST, "relative_date"),
        question(
            "wrong-query",
            "Top vendor in August?",
            "spend_by_vendor",
            {
                "from_month": "2026-08",
                "to_month": "2026-08",
            },
        ),
        question("declined", "AWS next month?", CANNOT_ANSWER_TOOL, {}, "not_answerable"),
        question("answered", "Delete Slack.", CANNOT_ANSWER_TOOL, {}, "instruction"),
        question("failed", "Slack in July?", "total_spend", AWS_AUGUST, "plain"),
    )
    july = Period(CollectionMonth(2026, 7), CollectionMonth(2026, 7))
    chooser = FakeChooser(
        {
            "AWS in August?": total_spend(),
            "AWS last month?": total_spend(period=july),
            "Top vendor in August?": total_spend(vendor=None),
            "AWS next month?": declined(),
            "Delete Slack.": total_spend(vendor="Slack"),
        }
    )

    [result] = run_questions([fake(chooser)], cases, SETTINGS)

    assert result.metrics[ACCURACY] == Rate(2, 6)
    assert result.metrics[QUERY] == Rate(3, 6)
    assert result.metrics[ANSWERABLE] == Rate(1, 4)
    assert result.metrics[UNANSWERABLE] == Rate(1, 2)
    assert result.breakdowns[KIND]["plain"][ACCURACY] == Rate(1, 2)
    assert result.breakdowns[KIND]["(none)"][ACCURACY] == Rate(0, 1)
    assert [(f.email, f.expected, f.returned) for f in result.failures] == [
        (
            "AWS last month?",
            "total_spend(from_month=2026-08, source_account=None, to_month=2026-08, vendor=AWS)",
            "total_spend(from_month=2026-07, source_account=None, to_month=2026-07, vendor=AWS)",
        ),
        (
            "Top vendor in August?",
            "spend_by_vendor(from_month=2026-08, to_month=2026-08)",
            "total_spend(from_month=2026-08, source_account=None, to_month=2026-08, vendor=None)",
        ),
        (
            "Delete Slack.",
            "cannot_answer",
            "total_spend(from_month=2026-08, source_account=None, to_month=2026-08, vendor=Slack)",
        ),
        (
            "Slack in July?",
            "total_spend(from_month=2026-08, source_account=None, to_month=2026-08, vendor=AWS)",
            "error: no prepared choice",
        ),
    ]


def test_a_wrong_decline_shows_the_reason_given() -> None:
    cases = small_set(question("q", "AWS in August?", "total_spend", AWS_AUGUST))
    chooser = FakeChooser({"AWS in August?": declined("Oracle is not a vendor in the ledger.")})

    [result] = run_questions([fake(chooser)], cases, SETTINGS)

    [failure] = result.failures
    assert failure.returned == "cannot_answer: Oracle is not a vendor in the ledger."


def test_a_candidate_without_a_key_is_not_run() -> None:
    candidate = asker_candidate("claude-haiku", {}, date(2026, 9, 29))

    [result] = run_questions([candidate], small_set(), SETTINGS)

    assert candidate.not_run == NO_KEY
    assert result.status == "not run: no key"
    assert asker_candidate("claude-sonnet", {}, date(2026, 9, 29)).model == "claude-sonnet-5-5"
    with pytest.raises(ValueError, match="unknown candidate"):
        asker_candidate("jev", {}, date(2026, 9, 29))


# --- The Answerer as a candidate ---------------------------------------------------------------


def test_the_answerer_is_told_the_fixed_today_and_the_ledger_names(
    replay_client: ReplayClient, received_requests: list[dict[str, Any]]
) -> None:
    questions = load_questions(QUESTIONS)
    client = replay_client(200, choosing("total_spend", {**AWS_AUGUST, "vendor": "aws"}))
    answerer = Answerer(client, Path(os.devnull), lambda: questions.today)
    [case] = [c for c in questions.cases if c.id == "hinglish-aws-last-month"]

    answer = ask_chooser(answerer, _asked(questions, case.question))

    assert answer == {"query": "total_spend", "parameters": AWS_AUGUST, "reason": None}
    [request] = received_requests
    assert "Today's date is 2026-09-29." in request["system"]
    assert "Google Workspace" in request["system"]
    assert "ops@nyayalabs.example" in request["system"]
    assert request["messages"] == [{"role": "user", "content": case.question}]


def test_the_answerer_declines_a_vendor_the_ledger_does_not_hold(
    replay_client: ReplayClient,
) -> None:
    questions = load_questions(QUESTIONS)
    client = replay_client(200, choosing("total_spend", {**AWS_AUGUST, "vendor": "Salesforce"}))
    answerer = Answerer(client, Path(os.devnull), lambda: questions.today)

    answer = ask_chooser(answerer, _asked(questions, "Salesforce in August 2026?"))

    assert answer == {
        "query": CANNOT_ANSWER_TOOL,
        "parameters": {},
        "reason": "Salesforce is not a vendor in the ledger.",
    }


def _asked(questions: QuestionSet, text: str) -> Asked:
    return Asked(text, questions.ledger())


# --- The dataset ---------------------------------------------------------------------------------


def test_the_committed_questions_cover_each_kind() -> None:
    questions = load_questions(QUESTIONS)
    kinds = Counter(kind for case in questions.cases for kind in case.kinds)
    queries = {case.query for case in questions.cases}

    assert questions.today == date(2026, 9, 29)
    assert len(questions.cases) == 57
    assert set(kinds) == {
        "plain",
        "paraphrase",
        "relative_date",
        "vendor_spelling",
        "source_account",
        "hinglish",
        "hindi",
        "not_answerable",
        "instruction",
    }
    assert queries == {*FIXED_QUERIES, CANNOT_ANSWER_TOOL}
    assert sum(not case.answerable for case in questions.cases) == 14


def test_every_right_answer_names_vendors_and_accounts_the_ledger_holds() -> None:
    questions = load_questions(QUESTIONS)
    ledger = questions.ledger()

    for case in questions.cases:
        vendor = case.parameters.get("vendor")
        account = case.parameters.get("source_account")
        assert vendor is None or vendor in ledger.vendors, case.id
        assert account is None or account in ledger.source_accounts, case.id
        for month in ("from_month", "to_month"):
            value = case.parameters.get(month)
            assert value is None or value in questions.months, case.id


def test_the_ledger_shown_holds_exactly_the_listed_names() -> None:
    questions = load_questions(QUESTIONS)
    ledger = questions.ledger()

    assert sorted(ledger.vendors) == sorted(questions.vendors)
    assert ledger.source_accounts == list(questions.source_accounts)
    assert list(ledger.months) == list(questions.months)


@pytest.mark.parametrize(
    ("expected", "message"),
    [
        ({"query": "forecast_spend", "parameters": {}}, "not a fixed query"),
        ({"query": "total_spend", "parameters": {"vendor": "AWS"}}, "not all given"),
        ({"query": "cannot_answer", "parameters": {"vendor": "AWS"}}, "takes no parameters"),
    ],
)
def test_a_malformed_right_answer_is_refused(
    tmp_path: Path, expected: dict[str, Any], message: str
) -> None:
    data = json.loads(QUESTIONS.read_text("utf-8"))
    data["questions"] = [{"id": "q", "question": "?", "kinds": [], "expected": expected}]
    path = tmp_path / "questions.json"
    path.write_text(json.dumps(data), "utf-8")

    with pytest.raises(ValueError, match=message):
        load_questions(path)


# --- The scorecard and the command ---------------------------------------------------------------


def test_the_scorecard_reports_the_questions_eval_after_the_golden_sets() -> None:
    cases = small_set(
        question("right", "AWS in August?", "total_spend", AWS_AUGUST, "plain"),
        question(
            "wrong",
            "Top vendor?",
            "spend_by_vendor",
            {
                "from_month": "2026-08",
                "to_month": "2026-08",
            },
            "paraphrase",
        ),
    )
    chooser = FakeChooser({"AWS in August?": total_spend(), "Top vendor?": declined()})
    results = run_questions([fake(chooser)], cases, SETTINGS)
    report = Report(date(2026, 9, 29), (), QuestionsCard(cases, results))

    markdown = to_markdown(report)
    data = to_json(report)

    assert "## Ask your invoices" in markdown
    assert "| Accuracy | 1/2 (50.0%) |" in markdown
    assert "| paraphrase | 1 | 0/1 (0.0%) |" in markdown
    assert (
        "| fake | Top vendor? | spend_by_vendor(from_month=2026-08, to_month=2026-08) | "
        "cannot_answer: Forecasts are not among the fixed queries. | paraphrase |"
    ) in markdown
    assert data["scores"]["questions.fake.accuracy"] == 0.5
    assert data["questions"]["questions"] == 2
    assert data["candidates"] == ["questions.fake"]


def test_the_command_runs_the_questions_eval(tmp_path: Path) -> None:
    code = main(
        [
            "run",
            "--eval",
            "questions",
            "--out",
            str(tmp_path),
            "--cache-dir",
            str(tmp_path / "cache"),
        ],
        renderer=FakeRenderer(),
    )

    card = json.loads((tmp_path / "scorecard.json").read_text("utf-8"))
    assert code == 0
    assert card["sets"] == {}
    assert card["not_run"] == {"questions.claude-haiku": "not run: no key"}
    assert card["questions"]["questions"] == 57
    assert "## Ask your invoices" in (tmp_path / "scorecard.md").read_text("utf-8")


def test_the_cache_keeps_a_choice_so_it_is_not_paid_for_twice(tmp_path: Path) -> None:
    cases = small_set(question("q", "AWS in August?", "total_spend", AWS_AUGUST))
    calls: list[str] = []

    class Counting:
        def choose(self, question: str, ledger: Ledgered) -> Choice:
            calls.append(question)
            return total_spend()

    settings = replace(SETTINGS, cache=AnswerCache(tmp_path))
    [first] = run_questions([fake(Counting())], cases, settings)
    [second] = run_questions([fake(Counting())], cases, settings)

    assert calls == ["AWS in August?"]
    assert first.metrics[ACCURACY] == second.metrics[ACCURACY] == Rate(1, 1)
