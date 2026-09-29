"""The Spend screen's API, called the way the dashboard calls it."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_api import FINANCE, sign_in
from three_months import DESIGN, ENGINEERING, JULY, OPS, record_three_months

from invoice_collector.api.app import create_app
from invoice_collector.api.identity import FakeIdentityVerifier
from invoice_collector.api.settings import Settings
from invoice_collector.domain import CollectionMonth, Email, EmailState
from invoice_collector.ledger import Ledger


@pytest.fixture
def ledger_path(tmp_path: Path) -> Path:
    return tmp_path / "out" / "ledger.sqlite"


@pytest.fixture
def dashboard(ledger_path: Path) -> Iterator[TestClient]:
    settings = Settings(
        session_secret="a-secret-only-for-tests",
        allowlist=frozenset({FINANCE}),
        ledger_path=ledger_path,
    )
    verifier = FakeIdentityVerifier({"code-finance": FINANCE})
    app = create_app(settings, lambda: Ledger(ledger_path), verifier)
    with TestClient(app, base_url="http://localhost:8000", follow_redirects=False) as client:
        sign_in(client)
        yield client


@pytest.fixture
def three_months(ledger_path: Path) -> None:
    ledger = Ledger(ledger_path)
    try:
        record_three_months(ledger)
    finally:
        ledger.close()


def test_spend_requires_a_signed_in_person(ledger_path: Path) -> None:
    settings = Settings(
        session_secret="a-secret-only-for-tests",
        allowlist=frozenset({FINANCE}),
        ledger_path=ledger_path,
    )
    app = create_app(settings, lambda: Ledger(ledger_path), FakeIdentityVerifier({}))
    with TestClient(app) as stranger:
        assert stranger.get("/api/spend").status_code == 401


@pytest.mark.usefixtures("three_months")
def test_spend_by_month_counts_each_charge_once_in_rupees(dashboard: TestClient) -> None:
    spend = dashboard.get("/api/spend").json()

    assert (spend["from_month"], spend["to_month"]) == ("2026-06", "2026-08")
    assert spend["months"] == [
        {"month": "2026-06", "inr_total": "12450.00"},
        {"month": "2026-07", "inr_total": "14280.00"},
        {"month": "2026-08", "inr_total": "17150.00"},
    ]
    assert spend["inr_total"] == "43880.00"


@pytest.mark.usefixtures("three_months")
def test_spend_by_vendor_is_ranked_and_credit_notes_reduce_it(dashboard: TestClient) -> None:
    spend = dashboard.get("/api/spend").json()

    assert spend["vendors"] == [
        {"vendor": "AWS", "inr_total": "31130.00"},
        {"vendor": "Slack", "inr_total": "12600.00"},
        {"vendor": "Linear", "inr_total": "1000.00"},
        {"vendor": "Figma", "inr_total": "-850.00"},
    ]


@pytest.mark.usefixtures("three_months")
def test_a_shared_charge_is_attributed_to_the_first_source_account(
    dashboard: TestClient,
) -> None:
    spend = dashboard.get("/api/spend").json()

    assert spend["source_accounts"] == [
        {"source_account": ENGINEERING, "inr_total": "31130.00"},
        {"source_account": DESIGN, "inr_total": "11750.00"},
        {"source_account": OPS, "inr_total": "1000.00"},
    ]
    assert spend["shared_charges"] == 1


@pytest.mark.usefixtures("three_months")
def test_charges_without_a_rupee_amount_are_reported_not_dropped(dashboard: TestClient) -> None:
    spend = dashboard.get("/api/spend").json()

    assert spend["without_rupees"] == {
        "charges": 1,
        "totals": [{"currency": "EUR", "amount": "20.00"}],
    }


@pytest.mark.usefixtures("three_months")
def test_change_from_the_previous_month_is_sorted_by_the_largest_change(
    dashboard: TestClient,
) -> None:
    spend = dashboard.get("/api/spend").json()

    assert spend["changes"] == {
        "month": "2026-08",
        "previous_month": "2026-07",
        "vendors": [
            {
                "vendor": "AWS",
                "previous_inr": "10080.00",
                "current_inr": "12750.00",
                "change_inr": "2670.00",
                "change_percent": "26.5",
            },
            {
                "vendor": "Linear",
                "previous_inr": "0.00",
                "current_inr": "1000.00",
                "change_inr": "1000.00",
                "change_percent": None,
            },
            {
                "vendor": "Figma",
                "previous_inr": "0.00",
                "current_inr": "-850.00",
                "change_inr": "-850.00",
                "change_percent": None,
            },
            {
                "vendor": "Slack",
                "previous_inr": "4200.00",
                "current_inr": "4250.00",
                "change_inr": "50.00",
                "change_percent": "1.2",
            },
        ],
    }


@pytest.mark.usefixtures("three_months")
def test_a_chosen_range_covers_only_its_months(dashboard: TestClient) -> None:
    spend = dashboard.get("/api/spend", params={"from": "2026-07", "to": "2026-07"}).json()

    assert spend["months"] == [{"month": "2026-07", "inr_total": "14280.00"}]
    assert spend["vendors"] == [
        {"vendor": "AWS", "inr_total": "10080.00"},
        {"vendor": "Slack", "inr_total": "4200.00"},
    ]
    assert spend["changes"]["previous_month"] == "2026-06"
    changes = spend["changes"]["vendors"]
    assert [(v["vendor"], v["change_inr"], v["change_percent"]) for v in changes] == [
        ("AWS", "1780.00", "21.4"),
        ("Slack", "50.00", "1.2"),
    ]


def test_default_range_is_the_latest_six_months_present(
    dashboard: TestClient, ledger_path: Path
) -> None:
    ledger = Ledger(ledger_path)
    try:
        for number in range(1, 9):
            ledger.record(
                CollectionMonth(2026, number),
                Email(
                    source_account=OPS,
                    message_id=f"m-{number}",
                    sender="someone@vendor.example",
                    subject="Hello",
                    received_at=JULY.start,
                ),
                EmailState.SKIPPED,
                reason="not a billing email",
            )
    finally:
        ledger.close()

    spend = dashboard.get("/api/spend").json()

    assert [m["month"] for m in spend["months"]] == [
        "2026-03",
        "2026-04",
        "2026-05",
        "2026-06",
        "2026-07",
        "2026-08",
    ]


def test_spend_with_an_empty_ledger(dashboard: TestClient, ledger_path: Path) -> None:
    response = dashboard.get("/api/spend")

    assert response.status_code == 200
    assert response.json() == {
        "from_month": None,
        "to_month": None,
        "months": [],
        "vendors": [],
        "source_accounts": [],
        "shared_charges": 0,
        "inr_total": "0.00",
        "without_rupees": {"charges": 0, "totals": []},
        "changes": None,
    }
    assert not ledger_path.exists()


@pytest.mark.parametrize(
    "params",
    [
        {"from": "2026-13"},
        {"to": "august"},
        {"from": "2026-8"},
        {"from": "2026-08", "to": "2026-07"},
    ],
)
def test_a_malformed_range_returns_422(dashboard: TestClient, params: dict[str, str]) -> None:
    assert dashboard.get("/api/spend", params=params).status_code == 422
