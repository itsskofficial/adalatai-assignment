"""The questions eval: does "Ask your invoices" choose the right fixed query (ADR 0007)?

Each question in plain words comes with the one fixed query and parameters that answer it,
or with cannot_answer where no fixed query fits or the question asks for something else.
The answer scored is what the server would run: the model's choice after the server's own
checks, so a vendor written in other letter case counts as the vendor it names, and a
vendor the ledger does not hold is declined as the dashboard would decline it. Scoring is
exact comparison (ADR 0011); no model judges anything.

The dataset fixes what today is, and the vendors, source accounts and collection months the
model is told the ledger holds, so relative dates such as "last month" have one right value.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from invoice_collector.api.questions import CANNOT_ANSWER_TOOL, FIXED_QUERIES, Ledgered
from invoice_collector.charge_history import Charge
from invoice_collector.domain import CollectionMonth, SummaryRow
from invoice_collector.evals.candidates import Candidate, QueryChooser
from invoice_collector.evals.runner import Answer, Call, Item, content_id
from invoice_collector.evals.scoring import (
    ACCURACY,
    CandidateResult,
    Failure,
    Rate,
    cost_and_time,
    label_breakdown,
    not_run,
)

QUESTIONS = "questions"
QUERY = "query"
ANSWERABLE = "answerable"
UNANSWERABLE = "unanswerable"
KIND = "question kind"

Parameters = dict[str, str | int | None]


@dataclass(frozen=True)
class QuestionCase:
    id: str
    question: str
    kinds: tuple[str, ...]
    query: str
    """The right fixed query, or cannot_answer."""
    parameters: Parameters

    @property
    def key(self) -> str:
        return self.id

    @property
    def answerable(self) -> bool:
        return self.query != CANNOT_ANSWER_TOOL

    def expected(self) -> str:
        return describe(self.query, self.parameters)


@dataclass(frozen=True)
class QuestionSet:
    today: date
    vendors: tuple[str, ...]
    source_accounts: tuple[str, ...]
    months: tuple[str, ...]
    cases: tuple[QuestionCase, ...]

    def ledger(self) -> Ledgered:
        """A ledger holding exactly these names, as the model is shown them."""
        return Ledgered(charges=_charges(self), months=self.months)


@dataclass(frozen=True)
class Asked:
    """What a candidate is given: the question and the ledger's names."""

    question: str
    ledger: Ledgered


def _charges(questions: QuestionSet) -> list[Charge]:
    """One charge per vendor, so the ledger lists every vendor and every source account.

    The model never sees an amount, so the amounts here are never read.
    """
    first = CollectionMonth.parse(questions.months[0])
    accounts = questions.source_accounts
    return [
        Charge(
            first,
            SummaryRow(
                vendor=vendor,
                document_type="invoice",
                invoice_date=date(first.year, first.month, 1),
                total=Decimal("0.00"),
                currency="USD",
                source_accounts=(accounts[n % len(accounts)],),
                file_link=f"{first}_{n}.pdf",
            ),
        )
        for n, vendor in enumerate(questions.vendors)
    ]


def _check(case: QuestionCase) -> None:
    if case.query == CANNOT_ANSWER_TOOL:
        if case.parameters:
            raise ValueError(f"{case.id}: cannot_answer takes no parameters")
        return
    query = FIXED_QUERIES.get(case.query)
    if query is None:
        raise ValueError(f"{case.id}: {case.query!r} is not a fixed query")
    if set(case.parameters) != {*query.required, *query.nullable}:
        raise ValueError(f"{case.id}: the parameters of {case.query} are not all given")


def load_questions(path: Path) -> QuestionSet:
    data = cast(dict[str, Any], json.loads(path.read_text("utf-8")))
    ledger = cast(dict[str, list[str]], data["ledger"])
    cases: list[QuestionCase] = []
    for entry in cast(list[dict[str, Any]], data["questions"]):
        expected = cast(dict[str, Any], entry["expected"])
        case = QuestionCase(
            id=str(entry["id"]),
            question=str(entry["question"]),
            kinds=tuple(sorted(str(kind) for kind in entry.get("kinds") or ())),
            query=str(expected["query"]),
            parameters=cast(Parameters, dict(expected.get("parameters") or {})),
        )
        _check(case)
        cases.append(case)
    ids = [case.id for case in cases]
    if len(set(ids)) != len(ids):
        raise ValueError("every question needs its own id")
    return QuestionSet(
        today=date.fromisoformat(str(data["today"])),
        vendors=tuple(ledger["vendors"]),
        source_accounts=tuple(ledger["source_accounts"]),
        months=tuple(sorted(ledger["months"])),
        cases=tuple(cases),
    )


def describe(query: str, parameters: Mapping[str, object]) -> str:
    """A choice written out for the scorecard: total_spend(vendor=AWS, from_month=2026-08)."""
    if query == CANNOT_ANSWER_TOOL:
        return CANNOT_ANSWER_TOOL
    given = ", ".join(f"{name}={parameters[name]}" for name in sorted(parameters))
    return f"{query}({given})"


def ask_chooser(chooser: QueryChooser, asked: Asked) -> Answer:
    choice = chooser.choose(asked.question, asked.ledger)
    return {"query": choice.name, "parameters": choice.parameters_used(), "reason": choice.reason}


def question_items(questions: QuestionSet) -> list[Item[Asked]]:
    ledger = questions.ledger()
    names = (
        questions.today.isoformat(),
        *ledger.vendors,
        *ledger.source_accounts,
        *questions.months,
    )
    return [
        Item(case.key, content_id(case.question, *names), Asked(case.question, ledger))
        for case in questions.cases
    ]


def question_tokens(asked: Asked) -> tuple[int, int]:
    """About 1,900 input tokens for the prompt and the eight tools, as a recorded call used."""
    return 1_900 + len(asked.question) // 4, 100


def _returned(answer: Answer) -> tuple[str, Parameters]:
    query = answer.get("query")
    parameters = answer.get("parameters")
    return (
        query if isinstance(query, str) else "",
        cast(Parameters, parameters) if isinstance(parameters, dict) else {},
    )


def score_questions(
    candidate: Candidate[Any], cases: Sequence[QuestionCase], calls: Mapping[str, Call]
) -> CandidateResult:
    """Exact comparison of the chosen query and its parameters with the right ones."""
    status = not_run(candidate, calls)
    if status:
        return CandidateResult(
            job=QUESTIONS,
            name=candidate.name,
            model=candidate.model,
            provider=candidate.provider,
            status=status,
        )

    right: dict[str, bool] = {}
    query_right = 0
    failures: list[Failure] = []
    for case in cases:
        call = calls.get(case.key)
        answer = call.answer if call else None
        query, parameters = _returned(answer) if answer is not None else ("", {})
        same_query = answer is not None and query == case.query
        is_right = same_query and (not case.answerable or parameters == case.parameters)
        query_right += same_query
        right[case.key] = is_right
        if not is_right:
            if answer is None:
                returned = "no answer" if call is None else f"error: {call.error}"
            else:
                returned = describe(query, parameters)
                reason = answer.get("reason")
                if query == CANNOT_ANSWER_TOOL and isinstance(reason, str) and reason:
                    returned += f": {reason}"
            failures.append(
                Failure(
                    QUESTIONS,
                    candidate.name,
                    case.question,
                    "choice",
                    case.expected(),
                    returned,
                    case.kinds,
                )
            )

    answerable = [c for c in cases if c.answerable]
    unanswerable = [c for c in cases if not c.answerable]
    return CandidateResult(
        job=QUESTIONS,
        name=candidate.name,
        model=candidate.model,
        provider=candidate.provider,
        status="ran",
        metrics={
            ACCURACY: Rate(sum(right.values()), len(cases)),
            QUERY: Rate(query_right, len(cases)),
            ANSWERABLE: Rate(sum(right[c.key] for c in answerable), len(answerable)),
            UNANSWERABLE: Rate(sum(right[c.key] for c in unanswerable), len(unanswerable)),
        },
        breakdowns={KIND: label_breakdown(((c.key, c.kinds) for c in cases), right)},
        failures=tuple(failures),
        cost=cost_and_time(candidate, calls.values()),
    )
