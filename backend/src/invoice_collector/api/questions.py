"""Ask your invoices: the model picks one fixed query, the server runs it and answers.

The model is given the question, today's date and the names of the vendors, source accounts
and collection months in the ledger. It never sees an amount. Everything it returns is
checked before a query runs, and anything that does not check out is declined, never
guessed at. The answer sentence is written here from the result, not by the model.
"""

import calendar
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, cast

import anthropic
from anthropic.types import ToolParam
from pydantic import BaseModel, Field

from invoice_collector import tracing
from invoice_collector.api.month_summary import file_name, file_url
from invoice_collector.charge_history import Charge
from invoice_collector.domain import CollectionMonth, DocumentType
from invoice_collector.fixed_queries import (
    Period,
    document_type_counts,
    largest_charges,
    new_vendors,
    spend_by_month,
    spend_by_vendor,
    total_spend,
    vendor_charges,
)

MODEL = "claude-sonnet-5-5"
CANNOT_ANSWER = "I can't answer that yet."
CANNOT_ANSWER_TOOL = "cannot_answer"
UNANSWERED_LOG = "unanswered_questions.jsonl"
MAX_LIMIT = 20
MONTH = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")

DOCUMENT_TYPE_NAMES: dict[DocumentType, tuple[str, str]] = {
    "invoice": ("invoice", "invoices"),
    "receipt": ("receipt", "receipts"),
    "credit_note": ("credit note", "credit notes"),
}

PROMPT = """You turn a question about a company's SaaS billing documents into a call to one \
of the tools you are given. Call exactly one tool, once. Do not answer in words.

Today's date is {today}. A collection month is written YYYY-MM. A period is given as \
from_month and to_month, both included. "Last month" is the calendar month before today's; \
"last quarter" is the three whole calendar months before the current quarter; "this year" \
runs from January of this year to the current month.

Vendors in the ledger: {vendors}
Source accounts in the ledger: {source_accounts}
Collection months in the ledger: {months}

Write a vendor or source account exactly as it is listed. If none of the tools can answer \
the question, or it names a vendor or source account that is not listed, call \
cannot_answer with a short reason."""


class QuestionsUnavailable(Exception):
    """Ask your invoices cannot run just now; the message says why."""


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=500)


class Column(BaseModel):
    key: str
    label: str
    kind: Literal["text", "amount", "count"]


class QueryUsed(BaseModel):
    name: str
    parameters: dict[str, str | int | None]
    description: str


class DocumentBehind(BaseModel):
    vendor: str
    document_type: DocumentType
    date: str
    amount: str
    currency: str
    amount_inr: str | None
    source_account: str
    file_name: str
    file_url: str


class Answer(BaseModel):
    question: str
    answered: bool
    answer: str
    reason: str | None
    query: QueryUsed | None
    columns: list[Column]
    rows: list[dict[str, str]]
    documents: list[DocumentBehind]


class CannotAnswer(Exception):
    """The model's choice cannot be run; the message is the reason given to the person."""


# Formatting ---------------------------------------------------------------------------------


def _amount(value: Decimal) -> str:
    return f"{value:.2f}"


def rupees(value: Decimal) -> str:
    """An amount in rupees with Indian digit grouping, as ₹1,23,456.00."""
    sign = "-" if value < 0 else ""
    whole, fraction = f"{abs(value):.2f}".split(".")
    last_three, rest = whole[-3:], whole[:-3]
    groups: list[str] = []
    while rest:
        groups.insert(0, rest[-2:])
        rest = rest[:-2]
    return f"{sign}₹{','.join([*groups, last_three])}.{fraction}"


def _month_name(month: CollectionMonth) -> str:
    return f"{calendar.month_name[month.month]} {month.year}"


def _day(day: date) -> str:
    return f"{day.day} {calendar.month_abbr[day.month]} {day.year}"


def _period_words(period: Period | None) -> str:
    """A period as a label: "June 2026 to August 2026"."""
    if period is None:
        return "all months"
    if period.first == period.last:
        return _month_name(period.first)
    return f"{_month_name(period.first)} to {_month_name(period.last)}"


def _within(period: Period | None) -> str:
    """A period inside a sentence: "from June 2026 to August 2026"."""
    if period is None:
        return "across all months"
    if period.first == period.last:
        return f"in {_month_name(period.first)}"
    return f"from {_period_words(period)}"


def _count(number: int, one: str, many: str) -> str:
    return f"{number} {one if number == 1 else many}"


def _listed(words: Sequence[str]) -> str:
    if len(words) <= 1:
        return "".join(words)
    return f"{', '.join(words[:-1])} and {words[-1]}"


def _left_out(charges: Sequence[Charge]) -> str:
    """A sentence about charges left out of a rupee figure, or nothing."""
    missing = [charge for charge in charges if charge.inr_total is None]
    if not missing:
        return ""
    totals: dict[str, Decimal] = {}
    for charge in missing:
        totals[charge.currency] = totals.get(charge.currency, Decimal(0)) + charge.total
    amounts = ", ".join(f"{currency} {_amount(totals[currency])}" for currency in sorted(totals))
    count = _count(len(missing), "charge", "charges")
    verb = "is" if len(missing) == 1 else "are"
    return f" {count} with no rupee amount {verb} not included ({amounts})."


def _document(charge: Charge) -> DocumentBehind:
    return DocumentBehind(
        vendor=charge.vendor,
        document_type=charge.document_type,
        date=charge.invoice_date.isoformat(),
        amount=_amount(charge.total),
        currency=charge.currency,
        amount_inr=_amount(charge.inr_total) if charge.inr_total is not None else None,
        source_account=charge.first_source_account,
        file_name=file_name(charge.file_link),
        file_url=file_url(charge.collection_month, charge.file_link),
    )


def _by_date(charges: Sequence[Charge]) -> list[Charge]:
    unique = list(dict.fromkeys(charges))
    return sorted(unique, key=lambda c: (c.invoice_date, c.vendor.casefold(), c.file_link))


# Checked parameters -------------------------------------------------------------------------


@dataclass(frozen=True)
class Parameters:
    vendor: str | None = None
    source_account: str | None = None
    period: Period | None = None
    limit: int = 5


@dataclass(frozen=True)
class Outcome:
    sentence: str
    description: str
    columns: list[Column]
    rows: list[dict[str, str]]
    charges: list[Charge]


@dataclass(frozen=True)
class Ledgered:
    """What the ledger holds, as the model and the checks see it."""

    charges: Sequence[Charge]
    months: Sequence[str]

    @property
    def vendors(self) -> list[str]:
        return sorted({c.vendor for c in self.charges}, key=str.casefold)

    @property
    def source_accounts(self) -> list[str]:
        return sorted({account for c in self.charges for account in c.source_accounts})


def _known(name: str, known: Sequence[str], what: str) -> str:
    for each in known:
        if each.casefold() == name.strip().casefold():
            return each
    raise CannotAnswer(f"{name} is not a {what} in the ledger.")


def _month(value: object) -> CollectionMonth:
    if not isinstance(value, str) or not MONTH.match(value):
        raise CannotAnswer(f"{value!r} is not a collection month.")
    return CollectionMonth.parse(value)


def _period(first: object, last: object, ledger: Ledgered) -> Period | None:
    if first is None and last is None:
        return None
    months = sorted(ledger.months)
    start = _month(first) if first is not None else _month(months[0] if months else last)
    end = _month(last) if last is not None else _month(months[-1] if months else first)
    if str(start) > str(end):
        raise CannotAnswer(f"The period starts in {start}, after it ends in {end}.")
    return Period(start, end)


# The fixed queries as tools -----------------------------------------------------------------

FIELDS: dict[str, tuple[str, str]] = {
    "vendor": ("string", "A vendor exactly as listed"),
    "source_account": ("string", "A source account exactly as listed"),
    "from_month": ("string", "First collection month of the period, YYYY-MM"),
    "to_month": ("string", "Last collection month of the period, YYYY-MM"),
    "limit": ("integer", f"How many charges to list, from 1 to {MAX_LIMIT}"),
}


@dataclass(frozen=True)
class FixedQuery:
    name: str
    description: str
    required: tuple[str, ...]
    nullable: tuple[str, ...]
    run: Callable[[Parameters, Sequence[Charge]], Outcome]

    def tool(self) -> ToolParam:
        properties: dict[str, Any] = {}
        for name in (*self.required, *self.nullable):
            kind, description = FIELDS[name]
            properties[name] = {
                "type": [kind, "null"] if name in self.nullable else kind,
                "description": description + (", or null" if name in self.nullable else ""),
            }
        return cast(
            ToolParam,
            {
                "name": self.name,
                "description": self.description,
                "strict": True,
                "input_schema": {
                    "type": "object",
                    "properties": properties,
                    "required": list(properties),
                    "additionalProperties": False,
                },
            },
        )


def _required_period(parameters: Parameters) -> Period:
    if parameters.period is None:
        raise CannotAnswer("The query needs a period.")
    return parameters.period


def _run_total_spend(parameters: Parameters, charges: Sequence[Charge]) -> Outcome:
    period = parameters.period
    result = total_spend(
        charges,
        vendor=parameters.vendor,
        source_account=parameters.source_account,
        period=period,
    )
    on = f" on {parameters.vendor}" if parameters.vendor else ""
    account = f" in source account {parameters.source_account}" if parameters.source_account else ""
    counted = len(result.charges) - len(result.without_rupees)
    return Outcome(
        sentence=(
            f"Total spend{on}{account} {_within(period)} was {rupees(result.inr_total)} "
            f"in {_count(counted, 'charge', 'charges')}.{_left_out(result.charges)}"
        ),
        description=f"Total spend{on}{account}, {_period_words(period)}",
        columns=[
            Column(key="inr_total", label="Total in rupees", kind="amount"),
            Column(key="charges", label="Charges", kind="count"),
            Column(key="without_rupees", label="Charges with no rupee amount", kind="count"),
        ],
        rows=[
            {
                "inr_total": _amount(result.inr_total),
                "charges": str(counted),
                "without_rupees": str(len(result.without_rupees)),
            }
        ],
        charges=list(result.charges),
    )


def _run_spend_by_vendor(parameters: Parameters, charges: Sequence[Charge]) -> Outcome:
    period = _required_period(parameters)
    ranked = spend_by_vendor(charges, period)
    everything = total_spend(charges, period=period)
    if ranked:
        top = ranked[0]
        sentence = (
            f"{top.key} had the most spend {_within(period)}: {rupees(top.inr_total)} of "
            f"{rupees(everything.inr_total)} across {_count(len(ranked), 'vendor', 'vendors')}."
        )
    else:
        sentence = f"No charges with a rupee amount were found {_within(period)}."
    return Outcome(
        sentence=sentence + _left_out(everything.charges),
        description=f"Spend by vendor, {_period_words(period)}",
        columns=[
            Column(key="vendor", label="Vendor", kind="text"),
            Column(key="inr_total", label="Spend in rupees", kind="amount"),
            Column(key="charges", label="Charges", kind="count"),
        ],
        rows=[
            {
                "vendor": each.key,
                "inr_total": _amount(each.inr_total),
                "charges": str(len(each.charges)),
            }
            for each in ranked
        ],
        charges=list(everything.charges),
    )


def _run_spend_by_month(parameters: Parameters, charges: Sequence[Charge]) -> Outcome:
    period = _required_period(parameters)
    vendor = parameters.vendor or ""
    months = spend_by_month(charges, vendor, period)
    everything = total_spend(charges, vendor=vendor, period=period)
    sentence = f"Spend on {vendor} {_within(period)} was {rupees(everything.inr_total)}"
    most = max(months, key=lambda each: each.inr_total, default=None)
    if most is not None and most.inr_total > 0 and len(months) > 1:
        most_month = _month_name(CollectionMonth.parse(most.key))
        sentence += f", the most in {most_month} ({rupees(most.inr_total)})"
    return Outcome(
        sentence=f"{sentence}.{_left_out(everything.charges)}",
        description=f"Spend by month on {vendor}, {_period_words(period)}",
        columns=[
            Column(key="month", label="Collection month", kind="text"),
            Column(key="inr_total", label="Spend in rupees", kind="amount"),
        ],
        rows=[
            {
                "month": _month_name(CollectionMonth.parse(each.key)),
                "inr_total": _amount(each.inr_total),
            }
            for each in months
        ],
        charges=list(everything.charges),
    )


def _run_largest_charges(parameters: Parameters, charges: Sequence[Charge]) -> Outcome:
    period = _required_period(parameters)
    largest = largest_charges(charges, period, parameters.limit)
    if largest:
        top = largest[0]
        sentence = (
            f"The largest charge {_within(period)} was {top.vendor} on {_day(top.invoice_date)} "
            f"for {rupees(top.inr_total or Decimal(0))}."
        )
    else:
        sentence = f"No charges with a rupee amount were found {_within(period)}."
    return Outcome(
        sentence=sentence + _left_out(total_spend(charges, period=period).charges),
        description=f"Largest {parameters.limit} charges, {_period_words(period)}",
        columns=[
            Column(key="vendor", label="Vendor", kind="text"),
            Column(key="date", label="Invoice date", kind="text"),
            Column(key="inr_total", label="Amount in rupees", kind="amount"),
        ],
        rows=[
            {
                "vendor": charge.vendor,
                "date": _day(charge.invoice_date),
                "inr_total": _amount(charge.inr_total or Decimal(0)),
            }
            for charge in largest
        ],
        charges=list(largest),
    )


def _run_vendor_charges(parameters: Parameters, charges: Sequence[Charge]) -> Outcome:
    period = _required_period(parameters)
    vendor = parameters.vendor or ""
    found = vendor_charges(charges, vendor, period)
    total = total_spend(found)
    if found:
        sentence = (
            f"{vendor} had {_count(len(found), 'charge', 'charges')} {_within(period)}, "
            f"totalling {rupees(total.inr_total)}.{_left_out(found)}"
        )
    else:
        sentence = f"No charges from {vendor} were found {_within(period)}."
    return Outcome(
        sentence=sentence,
        description=f"Charges from {vendor}, {_period_words(period)}",
        columns=[
            Column(key="date", label="Invoice date", kind="text"),
            Column(key="document_type", label="Document type", kind="text"),
            Column(key="amount", label="Amount", kind="text"),
            Column(key="inr_total", label="Amount in rupees", kind="amount"),
        ],
        rows=[
            {
                "date": _day(charge.invoice_date),
                "document_type": DOCUMENT_TYPE_NAMES[charge.document_type][0].capitalize(),
                "amount": f"{_amount(charge.total)} {charge.currency}",
                "inr_total": _amount(charge.inr_total) if charge.inr_total is not None else "",
            }
            for charge in found
        ],
        charges=list(found),
    )


def _run_new_vendors(parameters: Parameters, charges: Sequence[Charge]) -> Outcome:
    period = _required_period(parameters)
    found = new_vendors(charges, period)
    if found:
        names = [each.vendor for each in found]
        verb = "was" if len(found) == 1 else "were"
        sentence = (
            f"{_count(len(found), 'vendor', 'vendors')} {verb} first seen {_within(period)}: "
            f"{_listed(names)}."
        )
    else:
        sentence = f"No vendors were first seen {_within(period)}."
    return Outcome(
        sentence=sentence,
        description=f"Vendors first seen, {_period_words(period)}",
        columns=[
            Column(key="vendor", label="Vendor", kind="text"),
            Column(key="first_month", label="First collection month", kind="text"),
            Column(key="inr_total", label="Spend in rupees in the period", kind="amount"),
        ],
        rows=[
            {
                "vendor": each.vendor,
                "first_month": _month_name(each.first_month),
                "inr_total": _amount(total_spend(each.charges).inr_total),
            }
            for each in found
        ],
        charges=[charge for each in found for charge in each.charges],
    )


def _run_document_type_counts(parameters: Parameters, charges: Sequence[Charge]) -> Outcome:
    period = _required_period(parameters)
    counts = document_type_counts(charges, period)
    words = [
        _count(each.count, *DOCUMENT_TYPE_NAMES[each.document_type])
        for each in counts
        if each.count
    ]
    if words:
        sentence = f"There were {_listed(words)} {_within(period)}."
    else:
        sentence = f"No billing documents were collected {_within(period)}."
    return Outcome(
        sentence=sentence,
        description=f"Billing documents by document type, {_period_words(period)}",
        columns=[
            Column(key="document_type", label="Document type", kind="text"),
            Column(key="count", label="Billing documents", kind="count"),
        ],
        rows=[
            {
                "document_type": DOCUMENT_TYPE_NAMES[each.document_type][0].capitalize(),
                "count": str(each.count),
            }
            for each in counts
        ],
        charges=[charge for each in counts for charge in each.charges],
    )


PERIOD = ("from_month", "to_month")

FIXED_QUERIES: dict[str, FixedQuery] = {
    query.name: query
    for query in [
        FixedQuery(
            "total_spend",
            "Total spend in rupees, optionally on one vendor, from one source account, and in "
            "a period. Leave the period null for all months.",
            required=(),
            nullable=("vendor", "source_account", *PERIOD),
            run=_run_total_spend,
        ),
        FixedQuery(
            "spend_by_vendor",
            "Spend in rupees per vendor in a period, largest first.",
            required=PERIOD,
            nullable=(),
            run=_run_spend_by_vendor,
        ),
        FixedQuery(
            "spend_by_month",
            "Spend in rupees on one vendor in each collection month of a period.",
            required=("vendor", *PERIOD),
            nullable=(),
            run=_run_spend_by_month,
        ),
        FixedQuery(
            "largest_charges",
            "The largest charges in rupees in a period.",
            required=(*PERIOD, "limit"),
            nullable=(),
            run=_run_largest_charges,
        ),
        FixedQuery(
            "vendor_charges",
            "Every charge from one vendor in a period.",
            required=("vendor", *PERIOD),
            nullable=(),
            run=_run_vendor_charges,
        ),
        FixedQuery(
            "new_vendors",
            "Vendors whose first charge falls in a period.",
            required=PERIOD,
            nullable=(),
            run=_run_new_vendors,
        ),
        FixedQuery(
            "document_type_counts",
            "How many invoices, receipts and credit notes were collected in a period.",
            required=PERIOD,
            nullable=(),
            run=_run_document_type_counts,
        ),
    ]
}

CANNOT_ANSWER_TOOL_PARAM = cast(
    ToolParam,
    {
        "name": CANNOT_ANSWER_TOOL,
        "description": "Use when none of the other tools can answer the question.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "Why, in one short sentence"}
            },
            "required": ["reason"],
            "additionalProperties": False,
        },
    },
)


def tools() -> list[ToolParam]:
    return [query.tool() for query in FIXED_QUERIES.values()] + [CANNOT_ANSWER_TOOL_PARAM]


def _parameters(query: FixedQuery, given: object, ledger: Ledgered) -> Parameters:
    """Checks the parameters the model filled in against the query and the ledger."""
    if not isinstance(given, dict):
        raise CannotAnswer("The model did not fill in the query's parameters.")
    values = cast(dict[str, object], given)
    expected = {*query.required, *query.nullable}
    if set(values) != expected:
        raise CannotAnswer("The model filled in the wrong parameters for the query.")
    for name in query.required:
        if values[name] is None:
            raise CannotAnswer(f"The model left {name} empty.")
    for name in (*query.required, *query.nullable):
        value = values[name]
        kind = FIELDS[name][0]
        wrong_string = kind == "string" and not isinstance(value, str)
        whole_number = isinstance(value, int) and not isinstance(value, bool)
        wrong_integer = kind == "integer" and not whole_number
        if value is not None and (wrong_string or wrong_integer):
            raise CannotAnswer(f"The model gave {name} as {value!r}.")
    limit = values.get("limit", 5)
    if not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise CannotAnswer(f"The model asked for {limit!r} charges; choose 1 to {MAX_LIMIT}.")
    vendor = values.get("vendor")
    account = values.get("source_account")
    return Parameters(
        vendor=_known(vendor, ledger.vendors, "vendor") if isinstance(vendor, str) else None,
        source_account=(
            _known(account, ledger.source_accounts, "source account")
            if isinstance(account, str)
            else None
        ),
        period=_period(values.get("from_month"), values.get("to_month"), ledger),
        limit=limit,
    )


def _parameters_used(query: FixedQuery, parameters: Parameters) -> dict[str, str | int | None]:
    period = parameters.period
    everything: dict[str, str | int | None] = {
        "vendor": parameters.vendor,
        "source_account": parameters.source_account,
        "from_month": str(period.first) if period else None,
        "to_month": str(period.last) if period else None,
        "limit": parameters.limit,
    }
    return {name: everything[name] for name in (*query.required, *query.nullable)}


# Asking -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Choice:
    """The fixed query the model chose, with its parameters as checked, or why none can run."""

    query: FixedQuery | None
    parameters: Parameters | None
    reason: str | None = None

    @property
    def name(self) -> str:
        """The query's name, or cannot_answer when no query can run."""
        return self.query.name if self.query else CANNOT_ANSWER_TOOL

    def parameters_used(self) -> dict[str, str | int | None]:
        if self.query is None or self.parameters is None:
            return {}
        return _parameters_used(self.query, self.parameters)


def _chosen(response: object) -> dict[str, object]:
    """The queries the model chose, for its trace: each name and its parameters, without
    the reason it may give in its own words for declining."""
    chosen: list[dict[str, object]] = []
    for block in getattr(response, "content", []):
        if getattr(block, "type", None) == "tool_use":
            given = getattr(block, "input", None)
            parameters = (
                {k: v for k, v in cast(dict[str, object], given).items() if k != "reason"}
                if isinstance(given, dict)
                else {}
            )
            chosen.append({"query": getattr(block, "name", None), "parameters": parameters})
    return {"chosen": chosen}


class Answerer:
    def __init__(
        self,
        client: anthropic.Anthropic,
        log_path: Path,
        today: Callable[[], date],
        model: str = MODEL,
        tracer: tracing.Tracer = tracing.NO_TRACER,
    ) -> None:
        self._client = client
        self._log_path = log_path
        self._today = today
        self._model = model
        self._tracer = tracer

    def choose(self, question: str, ledger: Ledgered) -> Choice:
        """The query the model chose and its checked parameters; nothing is run or logged."""
        tool_uses = self._choose(question, ledger)
        try:
            if len(tool_uses) != 1:
                raise CannotAnswer("The model did not choose one of the fixed queries.")
            name, given = tool_uses[0]
            if name == CANNOT_ANSWER_TOOL:
                reason = (
                    cast(dict[str, object], given).get("reason")
                    if isinstance(given, dict)
                    else None
                )
                raise CannotAnswer(
                    reason.strip()
                    if isinstance(reason, str) and reason.strip()
                    else "The question is outside the fixed queries."
                )
            query = FIXED_QUERIES.get(name)
            if query is None:
                raise CannotAnswer(f"{name!r} is not one of the fixed queries.")
            return Choice(query, _parameters(query, given, ledger))
        except CannotAnswer as declined:
            return Choice(None, None, str(declined))

    def answer(self, question: str, ledger: Ledgered) -> Answer:
        choice = self.choose(question, ledger)
        try:
            query, parameters = choice.query, choice.parameters
            if query is None or parameters is None:
                raise CannotAnswer(choice.reason or "The question is outside the fixed queries.")
            outcome = query.run(parameters, ledger.charges)
        except CannotAnswer as declined:
            self._log(question, str(declined))
            return Answer(
                question=question,
                answered=False,
                answer=CANNOT_ANSWER,
                reason=str(declined),
                query=None,
                columns=[],
                rows=[],
                documents=[],
            )
        return Answer(
            question=question,
            answered=True,
            answer=outcome.sentence,
            reason=None,
            query=QueryUsed(
                name=query.name,
                parameters=_parameters_used(query, parameters),
                description=outcome.description,
            ),
            columns=outcome.columns,
            rows=outcome.rows,
            documents=[_document(charge) for charge in _by_date(outcome.charges)],
        )

    def _choose(self, question: str, ledger: Ledgered) -> list[tuple[str, object]]:
        """The tools the model called, by name with their input."""
        system = PROMPT.format(
            today=self._today().isoformat(),
            vendors=", ".join(ledger.vendors) or "none yet",
            source_accounts=", ".join(ledger.source_accounts) or "none yet",
            months=", ".join(sorted(ledger.months)) or "none yet",
        )
        try:
            response = tracing.traced(
                self._tracer,
                tracing.QUESTION,
                self._model,
                "anthropic",
                self._client.messages.create,
                output=_chosen,
                content=lambda: tracing.Content(text=f"{system}\n\n{question}"),
            )(
                model=self._model,
                max_tokens=1024,
                system=system,
                tools=tools(),
                tool_choice={"type": "auto", "disable_parallel_tool_use": True},
                messages=[{"role": "user", "content": question}],
            )
        except anthropic.APIConnectionError as error:
            raise QuestionsUnavailable(
                "The model could not be reached. Try again in a minute."
            ) from error
        except anthropic.APIStatusError as error:
            raise QuestionsUnavailable(
                f"The model could not answer just now (HTTP {error.status_code}). "
                "Try again in a minute."
            ) from error
        return [
            (block.name, cast(object, block.input))
            for block in response.content
            if block.type == "tool_use"
        ]

    def _log(self, question: str, reason: str) -> None:
        entry = {"asked_at": datetime.now(UTC).isoformat(), "question": question, "reason": reason}
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        with self._log_path.open("a", encoding="utf-8") as log:
            log.write(json.dumps(entry, ensure_ascii=False) + "\n")
