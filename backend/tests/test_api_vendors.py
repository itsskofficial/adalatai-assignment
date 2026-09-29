"""The Vendors screen's API, called the way the dashboard calls it."""

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from test_api import AUGUST, DESIGN, ENGINEERING, FINANCE, JULY, email, sign_in

from invoice_collector.api.app import create_app
from invoice_collector.api.identity import FakeIdentityVerifier
from invoice_collector.api.settings import Settings
from invoice_collector.charges import summarise
from invoice_collector.domain import (
    CollectionMonth,
    DocumentType,
    EmailState,
    ExpectedVendor,
    Extraction,
)
from invoice_collector.ledger import CollectedDocument, Ledger
from invoice_collector.reconciler import suggested_vendors

NOW = datetime(2026, 9, 29, 10, 30, tzinfo=UTC)


@pytest.fixture
def ledger_path(tmp_path: Path) -> Path:
    return tmp_path / "out" / "ledger.sqlite"


@pytest.fixture
def ledger(ledger_path: Path) -> Iterator[Ledger]:
    ledger = Ledger(ledger_path)
    yield ledger
    ledger.close()


@pytest.fixture
def app_client(ledger_path: Path) -> Iterator[TestClient]:
    settings = Settings(
        session_secret="a-secret-only-for-tests",
        allowlist=frozenset({FINANCE}),
        ledger_path=ledger_path,
    )
    verifier = FakeIdentityVerifier({"code-finance": FINANCE})
    app = create_app(settings, lambda: Ledger(ledger_path), verifier, now=lambda: NOW)
    with TestClient(app, base_url="http://localhost:8000", follow_redirects=False) as client:
        yield client


@pytest.fixture
def dashboard(app_client: TestClient) -> TestClient:
    sign_in(app_client)
    return app_client


def path_of(vendor: str) -> str:
    """A vendor's address, encoded the way the dashboard encodes it."""
    return f"/api/vendors/{quote(vendor, safe='')}"


def billed(
    ledger: Ledger,
    month: CollectionMonth,
    vendor: str,
    total: str,
    currency: str = "USD",
    *,
    day: int = 3,
    document_type: DocumentType = "invoice",
    account: str = ENGINEERING,
) -> None:
    """Records one collected billing document for a vendor in a collection month."""
    extraction = Extraction(
        document_type, vendor, date(month.year, month.month, day), Decimal(total), currency
    )
    message_id = f"m-{vendor}-{month}-{document_type}"
    ledger.record(
        month,
        email(message_id, f"{vendor} {document_type}", account=account, day=day, month=month.month),
        EmailState.COLLECTED,
        documents=(CollectedDocument(f"hash-{message_id}", extraction, f"{message_id}.pdf"),),
    )


def expected(vendor: str, **changes: Any) -> ExpectedVendor:
    fields: dict[str, Any] = {
        "vendor": vendor,
        "source_account": ENGINEERING,
        "billing_cycle": "monthly",
        "renewal_month": None,
        "usual_amount": None,
        "currency": "USD",
        "status": "expected",
    }
    fields.update(changes)
    return ExpectedVendor(**fields)


def listed(dashboard: TestClient) -> dict[str, Any]:
    response = dashboard.get("/api/vendors")
    assert response.status_code == 200
    return response.json()


def names(vendors: list[dict[str, Any]]) -> list[str]:
    return [vendor["vendor"] for vendor in vendors]


NEW_VENDOR: dict[str, Any] = {
    "vendor": "Linear",
    "source_account": ENGINEERING,
    "billing_cycle": "monthly",
    "renewal_month": None,
    "usual_amount": "96.00",
    "currency": "USD",
}


# Signing in


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/vendors"),
        ("POST", "/api/vendors"),
        ("PUT", "/api/vendors/Slack"),
        ("DELETE", "/api/vendors/Slack"),
        ("POST", "/api/vendors/Slack/accept"),
        ("POST", "/api/vendors/Slack/ignore"),
        ("POST", "/api/vendors/Slack/restore"),
        ("GET", "/api/vendors/Slack/history"),
    ],
)
def test_vendor_routes_return_401_when_not_signed_in(
    app_client: TestClient, ledger: Ledger, method: str, path: str
) -> None:
    ledger.save_expected_vendor(expected("Slack"))

    response = app_client.request(method, path, json=NEW_VENDOR)

    assert response.status_code == 401
    assert [v.vendor for v in ledger.expected_vendors()] == ["Slack"]


# The list


def test_vendors_are_grouped_by_status(dashboard: TestClient, ledger: Ledger) -> None:
    ledger.save_expected_vendor(expected("Slack"))
    ledger.save_expected_vendor(expected("AWS"))
    ledger.save_expected_vendor(expected("Notion", status="suggested"))
    ledger.save_expected_vendor(expected("Canva", status="ignored"))

    vendors = listed(dashboard)

    assert names(vendors["expected"]) == ["AWS", "Slack"]
    assert names(vendors["suggested"]) == ["Notion"]
    assert names(vendors["ignored"]) == ["Canva"]


def test_a_vendor_is_listed_with_what_the_list_holds_about_it(
    dashboard: TestClient, ledger: Ledger
) -> None:
    ledger.save_expected_vendor(
        expected(
            "Figma",
            source_account=DESIGN,
            billing_cycle="annual",
            renewal_month=3,
            usual_amount=Decimal("540.00"),
            currency="USD",
        )
    )

    (figma,) = listed(dashboard)["expected"]

    assert figma == {
        "vendor": "Figma",
        "status": "expected",
        "source_account": DESIGN,
        "billing_cycle": "annual",
        "renewal_month": 3,
        "usual_amount": "540.00",
        "currency": "USD",
        "months_billed": [],
        "latest_amount": None,
        "latest_currency": None,
        "gap": None,
    }


def test_the_list_is_empty_before_the_first_run(dashboard: TestClient) -> None:
    assert listed(dashboard) == {
        "latest_month": None,
        "expected": [],
        "suggested": [],
        "ignored": [],
    }


def test_months_billed_are_the_latest_six_under_any_spelling(
    dashboard: TestClient, ledger: Ledger
) -> None:
    ledger.save_expected_vendor(expected("Slack"))
    for month in range(1, 9):
        name = "Slack Technologies, Inc." if month % 2 else "Slack"
        billed(ledger, CollectionMonth(2026, month), name, f"{600 + month}.00")

    (slack,) = listed(dashboard)["expected"]

    assert slack["months_billed"] == [
        "2026-03",
        "2026-04",
        "2026-05",
        "2026-06",
        "2026-07",
        "2026-08",
    ]


def test_latest_amount_is_from_the_latest_charge_and_not_a_credit_note(
    dashboard: TestClient, ledger: Ledger
) -> None:
    ledger.save_expected_vendor(expected("Notion"))
    billed(ledger, JULY, "Notion", "200.00", "EUR")
    billed(ledger, AUGUST, "Notion", "221.40", "EUR", day=9)
    billed(ledger, AUGUST, "Notion", "-20.00", "EUR", day=20, document_type="credit_note")

    (notion,) = listed(dashboard)["expected"]

    assert (notion["latest_amount"], notion["latest_currency"]) == ("221.40", "EUR")
    assert notion["months_billed"] == ["2026-07", "2026-08"]


def test_a_gap_in_the_latest_collection_month_is_marked(
    dashboard: TestClient, ledger: Ledger
) -> None:
    ledger.save_expected_vendor(expected("Slack"))
    ledger.save_expected_vendor(expected("AWS"))
    ledger.save_expected_vendor(expected("Zoom", source_account=DESIGN))
    billed(ledger, JULY, "AWS", "1400.00")
    billed(ledger, AUGUST, "Slack", "652.50")
    ledger.record_sync(AUGUST, ENGINEERING)
    ledger.record_sync(AUGUST, DESIGN, reason="sign-in expired")

    vendors = listed(dashboard)

    assert vendors["latest_month"] == "2026-08"
    gaps = {vendor["vendor"]: vendor["gap"] for vendor in vendors["expected"]}
    assert gaps == {"AWS": "missing", "Slack": None, "Zoom": "unknown"}


def test_a_suggested_vendor_is_listed_with_what_is_known_about_it(
    dashboard: TestClient, ledger: Ledger
) -> None:
    billed(ledger, JULY, "Vercel", "20.00")
    billed(ledger, AUGUST, "Vercel", "24.00")
    charges = [row for m in ledger.months() for row in summarise(ledger.documents(m))]
    for suggestion in suggested_vendors(ledger.expected_vendors(), charges):
        ledger.save_expected_vendor(suggestion)

    (vercel,) = listed(dashboard)["suggested"]

    assert vercel["vendor"] == "Vercel"
    assert vercel["months_billed"] == ["2026-07", "2026-08"]
    assert (vercel["latest_amount"], vercel["latest_currency"]) == ("24.00", "USD")
    assert vercel["gap"] is None


# Adding


def test_an_expected_vendor_can_be_added(dashboard: TestClient, ledger: Ledger) -> None:
    response = dashboard.post("/api/vendors", json=NEW_VENDOR)

    assert response.status_code == 201
    assert ledger.expected_vendors() == [
        ExpectedVendor("Linear", ENGINEERING, "monthly", None, Decimal("96.00"), "USD")
    ]
    assert names(listed(dashboard)["expected"]) == ["Linear"]


def test_an_added_vendor_is_tidied(dashboard: TestClient, ledger: Ledger) -> None:
    dashboard.post(
        "/api/vendors",
        json={
            "vendor": "  Linear ",
            "source_account": " ",
            "billing_cycle": "annual",
            "renewal_month": 4,
            "usual_amount": "",
            "currency": "usd",
        },
    )

    assert ledger.expected_vendors() == [ExpectedVendor("Linear", None, "annual", 4, None, "USD")]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"vendor": "   "}, "The vendor name must not be blank"),
        ({"billing_cycle": "weekly"}, "The billing cycle must be monthly or annual"),
        (
            {"billing_cycle": "annual", "renewal_month": None},
            "An annual vendor needs a renewal month from 1 to 12",
        ),
        (
            {"billing_cycle": "annual", "renewal_month": 13},
            "An annual vendor needs a renewal month from 1 to 12",
        ),
        (
            {"billing_cycle": "annual", "renewal_month": 0},
            "An annual vendor needs a renewal month from 1 to 12",
        ),
        (
            {"billing_cycle": "annual", "renewal_month": "March"},
            "An annual vendor needs a renewal month from 1 to 12",
        ),
        ({"renewal_month": 3}, "A monthly vendor has no renewal month"),
        ({"usual_amount": "-5.00"}, "The usual amount must be a positive number"),
        ({"usual_amount": "0"}, "The usual amount must be a positive number"),
        ({"usual_amount": "ninety"}, "The usual amount must be a positive number"),
        ({"usual_amount": "NaN"}, "The usual amount must be a positive number"),
        ({"currency": "US"}, "The currency must be a three-letter code, such as USD"),
        ({"currency": "dollars"}, "The currency must be a three-letter code, such as USD"),
        ({"currency": "U$D"}, "The currency must be a three-letter code, such as USD"),
    ],
)
def test_a_vendor_that_breaks_a_rule_is_refused_with_a_plain_message(
    dashboard: TestClient, ledger: Ledger, changes: dict[str, Any], message: str
) -> None:
    response = dashboard.post("/api/vendors", json={**NEW_VENDOR, **changes})

    assert response.status_code == 422
    assert response.json() == {"detail": message}
    assert ledger.expected_vendors() == []


@pytest.mark.parametrize("status", ["expected", "suggested", "ignored"])
def test_a_vendor_already_listed_under_another_spelling_is_refused(
    dashboard: TestClient, ledger: Ledger, status: str
) -> None:
    ledger.save_expected_vendor(expected("Slack", status=status))

    response = dashboard.post(
        "/api/vendors", json={**NEW_VENDOR, "vendor": "Slack Technologies, Inc."}
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "Slack is already on the vendor list"}
    assert [v.vendor for v in ledger.expected_vendors()] == ["Slack"]


# Editing


def test_an_expected_vendor_can_be_edited(dashboard: TestClient, ledger: Ledger) -> None:
    ledger.save_expected_vendor(expected("Figma"))

    response = dashboard.put(
        "/api/vendors/Figma",
        json={
            "vendor": "Figma",
            "source_account": DESIGN,
            "billing_cycle": "annual",
            "renewal_month": 3,
            "usual_amount": "540",
            "currency": "USD",
        },
    )

    assert response.status_code == 200
    assert ledger.expected_vendors() == [
        ExpectedVendor("Figma", DESIGN, "annual", 3, Decimal("540"), "USD")
    ]


def test_renaming_a_vendor_leaves_no_old_entry(dashboard: TestClient, ledger: Ledger) -> None:
    ledger.save_expected_vendor(expected("Amazon Web Services"))

    response = dashboard.put(
        path_of("Amazon Web Services"), json={**NEW_VENDOR, "vendor": "AWS", "usual_amount": None}
    )

    assert response.status_code == 200
    assert [v.vendor for v in ledger.expected_vendors()] == ["AWS"]


def test_a_vendor_may_be_renamed_to_another_spelling_of_itself(
    dashboard: TestClient, ledger: Ledger
) -> None:
    ledger.save_expected_vendor(expected("slack"))

    response = dashboard.put("/api/vendors/slack", json={**NEW_VENDOR, "vendor": "Slack"})

    assert response.status_code == 200
    assert [v.vendor for v in ledger.expected_vendors()] == ["Slack"]


def test_renaming_onto_another_vendor_is_refused(dashboard: TestClient, ledger: Ledger) -> None:
    ledger.save_expected_vendor(expected("Slack"))
    ledger.save_expected_vendor(expected("Notion"))

    response = dashboard.put("/api/vendors/Notion", json={**NEW_VENDOR, "vendor": "SLACK Ltd"})

    assert response.status_code == 409
    assert [v.vendor for v in ledger.expected_vendors()] == ["Notion", "Slack"]


def test_an_edit_that_breaks_a_rule_is_refused(dashboard: TestClient, ledger: Ledger) -> None:
    ledger.save_expected_vendor(expected("Figma"))

    response = dashboard.put(
        "/api/vendors/Figma", json={**NEW_VENDOR, "vendor": "Figma", "billing_cycle": "annual"}
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "An annual vendor needs a renewal month from 1 to 12"}
    assert ledger.expected_vendors() == [expected("Figma")]


def test_editing_a_vendor_not_on_the_list_answers_404(dashboard: TestClient) -> None:
    response = dashboard.put("/api/vendors/Nobody", json=NEW_VENDOR)

    assert response.status_code == 404
    assert response.json() == {"detail": "Nobody is not on the vendor list"}


def test_a_name_with_a_slash_and_a_space_round_trips(dashboard: TestClient, ledger: Ledger) -> None:
    name = "Acme Tools / EU 100%?"
    dashboard.post("/api/vendors", json={**NEW_VENDOR, "vendor": name})

    edited = dashboard.put(path_of(name), json={**NEW_VENDOR, "vendor": name, "currency": "EUR"})
    history = dashboard.get(f"{path_of(name)}/history")
    removed = dashboard.delete(path_of(name))

    assert edited.status_code == 200
    assert [entry["action"] for entry in history.json()] == ["edited", "added"]
    assert history.json()[0]["after"]["currency"] == "EUR"
    assert removed.status_code == 204
    assert ledger.expected_vendors() == []


# Removing


def test_a_vendor_can_be_removed(dashboard: TestClient, ledger: Ledger) -> None:
    ledger.save_expected_vendor(expected("Slack"))
    ledger.save_expected_vendor(expected("Zoom"))

    response = dashboard.delete("/api/vendors/Zoom")

    assert response.status_code == 204
    assert [v.vendor for v in ledger.expected_vendors()] == ["Slack"]


def test_removing_a_vendor_not_on_the_list_answers_404(dashboard: TestClient) -> None:
    assert dashboard.delete("/api/vendors/Nobody").status_code == 404


# Suggested vendors


def test_accepting_a_suggested_vendor_makes_it_expected(
    dashboard: TestClient, ledger: Ledger
) -> None:
    ledger.save_expected_vendor(
        expected("Vercel", usual_amount=Decimal("20.00"), status="suggested")
    )

    response = dashboard.post("/api/vendors/Vercel/accept")

    assert response.status_code == 200
    assert ledger.expected_vendors() == [
        expected("Vercel", usual_amount=Decimal("20.00"), status="expected")
    ]


def test_a_suggested_vendor_can_be_accepted_with_changes(
    dashboard: TestClient, ledger: Ledger
) -> None:
    ledger.save_expected_vendor(
        expected("Vercel", usual_amount=Decimal("20.00"), status="suggested")
    )

    response = dashboard.post(
        "/api/vendors/Vercel/accept",
        json={"billing_cycle": "annual", "renewal_month": 6, "usual_amount": "240"},
    )

    assert response.status_code == 200
    assert ledger.expected_vendors() == [
        expected("Vercel", billing_cycle="annual", renewal_month=6, usual_amount=Decimal("240"))
    ]


def test_accepting_with_changes_that_break_a_rule_is_refused(
    dashboard: TestClient, ledger: Ledger
) -> None:
    ledger.save_expected_vendor(expected("Vercel", status="suggested"))

    response = dashboard.post("/api/vendors/Vercel/accept", json={"billing_cycle": "annual"})

    assert response.status_code == 422
    assert ledger.expected_vendors() == [expected("Vercel", status="suggested")]


def test_only_a_suggested_vendor_can_be_accepted_or_ignored(
    dashboard: TestClient, ledger: Ledger
) -> None:
    ledger.save_expected_vendor(expected("Slack"))

    accepted = dashboard.post("/api/vendors/Slack/accept")
    ignored = dashboard.post("/api/vendors/Slack/ignore")
    restored = dashboard.post("/api/vendors/Slack/restore")

    assert accepted.status_code == ignored.status_code == restored.status_code == 409
    assert accepted.json() == {"detail": "Slack is not a suggested vendor"}
    assert restored.json() == {"detail": "Slack is not an ignored vendor"}
    assert ledger.expected_vendors() == [expected("Slack")]


def test_an_ignored_vendor_is_not_suggested_again_until_restored(
    dashboard: TestClient, ledger: Ledger
) -> None:
    ledger.save_expected_vendor(expected("Vercel", status="suggested"))
    billed(ledger, AUGUST, "Vercel Inc", "24.00")
    charges = summarise(ledger.documents(AUGUST))

    ignored = dashboard.post("/api/vendors/Vercel/ignore")
    after_ignoring = listed(dashboard)
    suggested_after_ignoring = suggested_vendors(ledger.expected_vendors(), charges)
    restored = dashboard.post("/api/vendors/Vercel/restore")

    assert ignored.status_code == restored.status_code == 200
    assert names(after_ignoring["ignored"]) == ["Vercel"]
    assert after_ignoring["suggested"] == []
    assert suggested_after_ignoring == []
    assert names(listed(dashboard)["suggested"]) == ["Vercel"]


def test_accepting_a_vendor_not_on_the_list_answers_404(dashboard: TestClient) -> None:
    assert dashboard.post("/api/vendors/Nobody/accept").status_code == 404


# History


def test_every_change_is_recorded_with_who_made_it_and_when(
    dashboard: TestClient, ledger: Ledger
) -> None:
    ledger.save_expected_vendor(expected("Vercel", status="suggested"))

    dashboard.post("/api/vendors/Vercel/ignore")
    dashboard.post("/api/vendors/Vercel/restore")
    dashboard.post("/api/vendors/Vercel/accept", json={"usual_amount": "24"})
    response = dashboard.get("/api/vendors/Vercel/history")

    assert response.status_code == 200
    history = response.json()
    assert [entry["action"] for entry in history] == ["accepted", "restored", "ignored"]
    assert {entry["person"] for entry in history} == {FINANCE}
    assert {entry["changed_at"] for entry in history} == {NOW.isoformat()}
    assert history[0]["vendor"] == "Vercel"
    assert history[0]["before"] == {
        "vendor": "Vercel",
        "source_account": ENGINEERING,
        "billing_cycle": "monthly",
        "renewal_month": None,
        "usual_amount": None,
        "currency": "USD",
        "status": "suggested",
    }
    assert history[0]["after"] == {
        **history[0]["before"],
        "usual_amount": "24",
        "status": "expected",
    }


def test_history_records_additions_and_removals(dashboard: TestClient) -> None:
    dashboard.post("/api/vendors", json=NEW_VENDOR)
    dashboard.delete("/api/vendors/Linear")

    history = dashboard.get("/api/vendors/Linear/history").json()

    assert [entry["action"] for entry in history] == ["removed", "added"]
    assert history[0]["after"] is None
    assert history[1]["before"] is None
    assert history[1]["after"]["usual_amount"] == "96.00"


def test_history_follows_a_vendor_across_a_rename(dashboard: TestClient) -> None:
    dashboard.post("/api/vendors", json={**NEW_VENDOR, "vendor": "Amazon Web Services"})
    dashboard.put(path_of("Amazon Web Services"), json={**NEW_VENDOR, "vendor": "AWS"})

    history = dashboard.get("/api/vendors/AWS/history").json()

    assert [entry["action"] for entry in history] == ["edited", "added"]
    assert history[0]["before"]["vendor"] == "Amazon Web Services"
    assert history[0]["after"]["vendor"] == "AWS"


def test_a_refused_change_is_not_recorded(dashboard: TestClient) -> None:
    dashboard.post("/api/vendors", json={**NEW_VENDOR, "currency": "dollars"})

    assert dashboard.get("/api/vendors/Linear/history").json() == []
