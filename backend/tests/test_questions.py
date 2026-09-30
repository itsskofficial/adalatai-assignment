"""Ask your invoices, called the way the dashboard calls it.

The model is never called: the Claude client is pointed at a local server replaying
hand-written tool-use responses in the shape the API returns.
"""

import json
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import anthropic
import pytest
from conftest import ReplayClient
from fastapi.testclient import TestClient
from test_api import FINANCE, sign_in
from three_months import AUGUST, DESIGN, ENGINEERING, OPS, record_three_months

from invoice_collector.api.app import create_app
from invoice_collector.api.identity import FakeIdentityVerifier
from invoice_collector.api.settings import Settings
from invoice_collector.domain import Email, EmailState, Extraction
from invoice_collector.ledger import CollectedDocument, Ledger

TOOL_USE: dict[str, Any] = json.loads(
    (Path(__file__).parent / "recorded/claude_question_tool_use.json").read_text("utf-8")
)
CANNOT = "I can't answer that yet."

Dashboard = Callable[[anthropic.Anthropic | None], TestClient]
Ask = Callable[[int, dict[str, Any]], TestClient]


def choosing(name: str, parameters: dict[str, Any]) -> dict[str, Any]:
    """The hand-written response, with the model choosing this tool and these parameters."""
    block = {**TOOL_USE["content"][0], "name": name, "input": parameters}
    return {**TOOL_USE, "content": [block]}


def answering_in_words(text: str) -> dict[str, Any]:
    """A response in which the model wrote text and called no tool."""
    block = {"type": "text", "text": text, "citations": None}
    return {**TOOL_USE, "stop_reason": "end_turn", "content": [block]}


@pytest.fixture
def ledger_path(tmp_path: Path) -> Path:
    path = tmp_path / "out" / "ledger.sqlite"
    ledger = Ledger(path)
    try:
        record_three_months(ledger)
    finally:
        ledger.close()
    return path


@pytest.fixture
def log_path(ledger_path: Path) -> Path:
    return ledger_path.parent / "unanswered_questions.jsonl"


@pytest.fixture
def dashboard(ledger_path: Path) -> Iterator[Dashboard]:
    clients: list[TestClient] = []

    def open_with(claude: anthropic.Anthropic | None) -> TestClient:
        settings = Settings(
            session_secret="a-secret-only-for-tests",
            allowlist=frozenset({FINANCE}),
            ledger_path=ledger_path,
        )
        app = create_app(
            settings,
            lambda: Ledger(ledger_path),
            FakeIdentityVerifier({"code-finance": FINANCE}),
            claude=claude,
            today=lambda: date(2026, 9, 29),
        )
        client = TestClient(app, base_url="http://localhost:8000", follow_redirects=False)
        client.__enter__()
        clients.append(client)
        sign_in(client)
        return client

    yield open_with
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def ask(dashboard: Dashboard, replay_client: ReplayClient) -> Ask:
    return lambda status, body: dashboard(replay_client(status, body))


def question(client: TestClient, text: str) -> dict[str, Any]:
    response = client.post("/api/questions", json={"question": text})
    assert response.status_code == 200, response.text
    return response.json()


def test_questions_require_a_signed_in_person(ledger_path: Path) -> None:
    settings = Settings(
        session_secret="a-secret-only-for-tests",
        allowlist=frozenset({FINANCE}),
        ledger_path=ledger_path,
    )
    app = create_app(settings, lambda: Ledger(ledger_path), FakeIdentityVerifier({}))
    with TestClient(app) as stranger:
        response = stranger.post("/api/questions", json={"question": "How much on AWS?"})

    assert response.status_code == 401


def test_total_spend_on_a_vendor_is_answered_by_the_server(ask: Ask) -> None:
    client = ask(200, TOOL_USE)

    answer = question(client, "How much did we spend on AWS this summer?")

    assert answer["answered"] is True
    assert answer["answer"] == (
        "Total spend on AWS from June 2026 to August 2026 was ₹31,130.00 in 3 charges."
    )
    assert answer["query"] == {
        "name": "total_spend",
        "parameters": {
            "vendor": "AWS",
            "source_account": None,
            "from_month": "2026-06",
            "to_month": "2026-08",
        },
        "description": "Total spend on AWS, June 2026 to August 2026",
    }
    assert answer["rows"] == [{"inr_total": "31130.00", "charges": "3", "without_rupees": "0"}]


def test_an_answer_lists_the_billing_documents_behind_it(ask: Ask) -> None:
    client = ask(200, TOOL_USE)

    answer = question(client, "How much did we spend on AWS this summer?")

    assert answer["documents"] == [
        {
            "vendor": "AWS",
            "document_type": "invoice",
            "date": f"2026-0{month}-02",
            "amount": amount,
            "currency": "USD",
            "amount_inr": inr,
            "source_account": ENGINEERING,
            "file_name": f"2026-0{month}_AWS_{amount}-USD.pdf",
            "file_url": (
                f"/api/months/2026-0{month}/billing-documents/2026-0{month}_AWS_{amount}-USD.pdf"
            ),
        }
        for month, amount, inr in [
            (6, "100.00", "8300.00"),
            (7, "120.00", "10080.00"),
            (8, "150.00", "12750.00"),
        ]
    ]


def test_a_document_in_google_drive_is_named_as_the_run_names_its_file(
    ledger_path: Path, ask: Ask
) -> None:
    drive_link = "https://drive.google.com/file/d/1awsAug/view?usp=drivesdk"
    ledger = Ledger(ledger_path)
    try:
        ledger.record(
            AUGUST,
            Email(
                source_account=ENGINEERING,
                message_id="m-aws-aug-support",
                sender="AWS <billing@aws.example>",
                subject="Your AWS support invoice",
                received_at=datetime(2026, 8, 20, 9, 0, tzinfo=UTC),
            ),
            EmailState.COLLECTED,
            documents=(
                CollectedDocument(
                    "hash-of-aws-support",
                    Extraction("invoice", "AWS", date(2026, 8, 20), Decimal("40"), "USD"),
                    drive_link,
                    Decimal("85"),
                ),
            ),
        )
    finally:
        ledger.close()
    client = ask(200, TOOL_USE)

    answer = question(client, "How much did we spend on AWS this summer?")

    [in_drive] = [d for d in answer["documents"] if d["file_url"] == drive_link]
    assert in_drive["file_name"] == "2026-08_AWS_40.00-USD.pdf"


def test_spend_by_vendor_is_answered(ask: Ask) -> None:
    client = ask(200, choosing("spend_by_vendor", {"from_month": "2026-08", "to_month": "2026-08"}))

    answer = question(client, "Which vendor cost the most last month?")

    assert answer["answer"] == (
        "AWS had the most spend in August 2026: ₹12,750.00 of ₹17,150.00 across 4 vendors."
    )
    assert answer["query"]["description"] == "Spend by vendor, August 2026"
    assert [(row["vendor"], row["inr_total"]) for row in answer["rows"]] == [
        ("AWS", "12750.00"),
        ("Slack", "4250.00"),
        ("Linear", "1000.00"),
        ("Figma", "-850.00"),
    ]
    assert [column["kind"] for column in answer["columns"]] == ["text", "amount", "count"]
    assert len(answer["documents"]) == 4


def test_vendors_first_seen_are_answered(ask: Ask) -> None:
    client = ask(200, choosing("new_vendors", {"from_month": "2026-08", "to_month": "2026-08"}))

    answer = question(client, "Which vendors are new in August?")

    assert answer["answer"] == "2 vendors were first seen in August 2026: Figma and Linear."
    assert [row["vendor"] for row in answer["rows"]] == ["Figma", "Linear"]
    assert [document["vendor"] for document in answer["documents"]] == ["Linear", "Figma"]


def test_spend_by_month_for_a_vendor_is_answered(ask: Ask) -> None:
    client = ask(
        200,
        choosing(
            "spend_by_month", {"vendor": "Slack", "from_month": "2026-06", "to_month": "2026-08"}
        ),
    )

    answer = question(client, "Slack spend by month this summer?")

    assert answer["answer"] == (
        "Spend on Slack from June 2026 to August 2026 was ₹12,600.00, "
        "the most in August 2026 (₹4,250.00)."
    )
    assert [(row["month"], row["inr_total"]) for row in answer["rows"]] == [
        ("June 2026", "4150.00"),
        ("July 2026", "4200.00"),
        ("August 2026", "4250.00"),
    ]


def test_largest_charges_are_answered(ask: Ask) -> None:
    client = ask(
        200,
        choosing("largest_charges", {"from_month": "2026-07", "to_month": "2026-08", "limit": 2}),
    )

    answer = question(client, "What were our two biggest charges since July?")

    assert answer["answer"] == (
        "The largest charge from July 2026 to August 2026 was AWS on 2 Aug 2026 for ₹12,750.00. "
        "1 charge with no rupee amount is not included (EUR 20.00)."
    )
    assert [row["inr_total"] for row in answer["rows"]] == ["12750.00", "10080.00"]


def test_a_shared_charge_counts_under_the_first_source_account(ask: Ask) -> None:
    client = ask(
        200,
        choosing(
            "total_spend",
            {"vendor": None, "source_account": DESIGN, "from_month": None, "to_month": None},
        ),
    )

    answer = question(client, "How much has the design mailbox spent?")

    # Slack in June, July and August, less the Figma credit note.
    assert answer["answer"] == (
        f"Total spend in source account {DESIGN} across all months was ₹11,750.00 "
        "in 4 charges. 1 charge with no rupee amount is not included (EUR 20.00)."
    )


def test_counts_by_document_type_are_answered(ask: Ask) -> None:
    client = ask(
        200,
        choosing("document_type_counts", {"from_month": "2026-06", "to_month": "2026-08"}),
    )

    answer = question(client, "How many credit notes did we get?")

    assert answer["answer"] == (
        "There were 7 invoices, 1 receipt and 1 credit note from June 2026 to August 2026."
    )


def test_a_vendor_named_in_other_letter_case_is_the_vendor_in_the_ledger(ask: Ask) -> None:
    client = ask(
        200,
        choosing(
            "vendor_charges", {"vendor": "linear", "from_month": "2026-06", "to_month": "2026-08"}
        ),
    )

    answer = question(client, "Show me Linear's charges")

    assert answer["query"]["parameters"]["vendor"] == "Linear"
    assert answer["answer"] == (
        "Linear had 1 charge from June 2026 to August 2026, totalling ₹1,000.00."
    )
    assert answer["documents"][0]["source_account"] == OPS


def test_the_model_is_given_tools_and_names_but_never_amounts(
    ask: Ask, received_requests: list[dict[str, Any]]
) -> None:
    question(ask(200, TOOL_USE), "How much did we spend on AWS this summer?")

    sent = received_requests[0]
    assert sent["model"] == "claude-sonnet-5-5"
    assert sent["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
    assert {tool["name"] for tool in sent["tools"]} == {
        "total_spend",
        "spend_by_vendor",
        "spend_by_month",
        "largest_charges",
        "vendor_charges",
        "new_vendors",
        "document_type_counts",
        "cannot_answer",
    }
    for tool in sent["tools"]:
        schema = tool["input_schema"]
        assert tool["strict"] is True
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
    words = json.dumps(sent)
    assert "2026-09-29" in words
    assert all(name in words for name in ("AWS", "Slack", "Notion", ENGINEERING, OPS))
    assert not any(figure in words for figure in ("12750", "12,750", "8300", "31130"))


def test_unknown_vendor_from_the_model_cannot_be_answered(ask: Ask) -> None:
    client = ask(
        200,
        choosing(
            "total_spend",
            {"vendor": "Oracle", "source_account": None, "from_month": None, "to_month": None},
        ),
    )

    answer = question(client, "How much did we spend on Oracle?")

    assert answer["answered"] is False
    assert answer["answer"] == CANNOT
    assert answer["reason"] == "Oracle is not a vendor in the ledger."
    assert (answer["query"], answer["rows"], answer["documents"]) == (None, [], [])


def test_unknown_source_account_from_the_model_cannot_be_answered(ask: Ask) -> None:
    client = ask(
        200,
        choosing(
            "total_spend",
            {"vendor": None, "source_account": "x@y.example", "from_month": None, "to_month": None},
        ),
    )

    answer = question(client, "How much did x spend?")

    assert answer["reason"] == "x@y.example is not a source account in the ledger."


@pytest.mark.parametrize("month", ["2026-13", "August", "2026-8", "", "2026-08-01"])
def test_malformed_month_from_the_model_cannot_be_answered(ask: Ask, month: str) -> None:
    client = ask(200, choosing("spend_by_vendor", {"from_month": month, "to_month": "2026-08"}))

    answer = question(client, "Spend by vendor?")

    assert answer["answered"] is False
    assert answer["reason"] == f"{month!r} is not a collection month."


def test_period_that_ends_before_it_starts_cannot_be_answered(ask: Ask) -> None:
    client = ask(200, choosing("spend_by_vendor", {"from_month": "2026-08", "to_month": "2026-06"}))

    answer = question(client, "Spend by vendor?")

    assert answer["reason"] == "The period starts in 2026-08, after it ends in 2026-06."


@pytest.mark.parametrize(
    ("name", "parameters"),
    [
        ("run_sql", {"sql": "SELECT * FROM billing_documents"}),
        ("spend_by_vendor", {"from_month": "2026-06"}),
        ("spend_by_vendor", {"from_month": "2026-06", "to_month": "2026-08", "extra": 1}),
        ("largest_charges", {"from_month": "2026-06", "to_month": "2026-08", "limit": "five"}),
        ("largest_charges", {"from_month": "2026-06", "to_month": "2026-08", "limit": 0}),
        ("spend_by_month", {"vendor": None, "from_month": "2026-06", "to_month": "2026-08"}),
    ],
)
def test_a_query_the_server_does_not_recognise_cannot_be_answered(
    ask: Ask, name: str, parameters: dict[str, Any]
) -> None:
    answer = question(ask(200, choosing(name, parameters)), "Anything?")

    assert answer["answered"] is False
    assert answer["answer"] == CANNOT


def test_the_cannot_answer_tool_is_passed_on_plainly(ask: Ask) -> None:
    client = ask(
        200, choosing("cannot_answer", {"reason": "Forecasts are not among the fixed queries."})
    )

    answer = question(client, "What will we spend next year?")

    assert answer == {
        "question": "What will we spend next year?",
        "answered": False,
        "answer": CANNOT,
        "reason": "Forecasts are not among the fixed queries.",
        "query": None,
        "columns": [],
        "rows": [],
        "documents": [],
    }


def test_a_response_with_no_tool_call_cannot_be_answered(ask: Ask) -> None:
    client = ask(200, answering_in_words("You spent ₹99,999 on AWS."))

    answer = question(client, "How much did we spend on AWS?")

    assert answer["answered"] is False
    assert answer["reason"] == "The model did not choose one of the fixed queries."
    assert "99,999" not in json.dumps(answer)


def test_unanswered_questions_are_logged_next_to_the_ledger(ask: Ask, log_path: Path) -> None:
    client = ask(200, choosing("cannot_answer", {"reason": "Forecasts are not supported."}))

    question(client, "What will we spend next year?")
    question(client, "And the year after?")

    logged = [json.loads(line) for line in log_path.read_text("utf-8").splitlines()]
    assert [(entry["question"], entry["reason"]) for entry in logged] == [
        ("What will we spend next year?", "Forecasts are not supported."),
        ("And the year after?", "Forecasts are not supported."),
    ]
    assert all(entry["asked_at"] for entry in logged)


def test_answered_questions_are_not_logged(ask: Ask, log_path: Path) -> None:
    question(ask(200, TOOL_USE), "How much did we spend on AWS this summer?")

    assert not log_path.exists()


def test_a_model_failure_returns_503(ask: Ask, log_path: Path) -> None:
    error = {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
    client = ask(529, error)

    response = client.post("/api/questions", json={"question": "How much on AWS?"})

    assert response.status_code == 503
    assert response.json()["detail"] == (
        "The model could not answer just now (HTTP 529). Try again in a minute."
    )
    assert not log_path.exists()


def test_without_an_api_key_questions_return_503_and_the_rest_works(
    dashboard: Dashboard,
) -> None:
    client = dashboard(None)

    response = client.post("/api/questions", json={"question": "How much on AWS?"})

    assert response.status_code == 503
    assert "ANTHROPIC_API_KEY is not set" in response.json()["detail"]
    assert client.get("/api/spend").status_code == 200
    assert client.get("/api/months").status_code == 200


@pytest.mark.parametrize("body", [{}, {"question": ""}, {"question": "x" * 501}])
def test_a_missing_or_overlong_question_returns_422(ask: Ask, body: dict[str, str]) -> None:
    client = ask(200, TOOL_USE)

    assert client.post("/api/questions", json=body).status_code == 422
