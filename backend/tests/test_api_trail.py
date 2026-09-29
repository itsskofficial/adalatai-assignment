"""The history of one billing document, through the dashboard's API."""

from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from support import Collection
from test_api import FINANCE, sign_in
from test_api_review import (
    NOW,
    PENDING_NAME,
    SLACK,
    SLACK_PDF,
    FakeDriveArchive,
    action_path,
    approve,
    hold_slack,
    hold_slack_in_drive,
    open_dashboard,
    slack_email,
)

from invoice_collector.exchange_rates import FakeExchangeRates
from invoice_collector.extractor import content_hash

SLACK_HASH = content_hash(SLACK_PDF)
TRAIL = f"/api/billing-documents/{SLACK_HASH}/trail"


@pytest.fixture
def dashboard(tmp_path: Path) -> Iterator[TestClient]:
    rates = FakeExchangeRates({"USD": Decimal("95.34")})
    clock: list[datetime] = [NOW]
    with open_dashboard(tmp_path, rates, clock) as client:
        sign_in(client)
        yield client


def history(dashboard: TestClient) -> dict[str, Any]:
    response = dashboard.get(TRAIL)
    assert response.status_code == 200
    return response.json()


def kinds(trail: dict[str, Any]) -> list[str]:
    return [entry["kind"] for entry in trail["entries"]]


def test_each_summary_row_names_its_document_for_its_history(
    collection: Collection, dashboard: TestClient
) -> None:
    collection.answers[SLACK_PDF] = replace(SLACK, by="claude-haiku-4-5")
    collection.run([slack_email()])

    [row] = dashboard.get("/api/months/2026-08/summary").json()["rows"]

    assert row["content_hash"] == SLACK_HASH


def test_history_of_a_collected_document(collection: Collection, dashboard: TestClient) -> None:
    collection.answers[SLACK_PDF] = replace(SLACK, by="claude-haiku-4-5")
    email = slack_email()
    collection.run([email])

    trail = history(dashboard)

    assert trail["state"] == "collected"
    assert trail["collection_month"] == "2026-08"
    assert trail["invoice_format"] == "attachment"
    assert trail["file_url"] == (
        "/api/months/2026-08/billing-documents/2026-08_Slack_652.50-USD.pdf"
    )
    [received] = trail["emails"]
    assert (received["sender"], received["subject"]) == (email.sender, email.subject)
    [read] = [e for e in trail["entries"] if e["kind"] == "read"]
    assert read["actor"] == "claude-haiku-4-5"
    assert "checked" in kinds(trail)


def test_each_correction_shows_who_when_and_the_value_before_and_after(
    collection: Collection, dashboard: TestClient
) -> None:
    hold_slack(collection)

    assert approve(dashboard, slack_email(), total="625.50").status_code == 200

    trail = history(dashboard)
    assert kinds(trail)[-6:] == [
        "held",
        "corrected",
        "approved",
        "filed",
        "converted",
        "pending_copy_removed",
    ]
    [corrected] = [e for e in trail["entries"] if e["kind"] == "corrected"]
    assert corrected["actor"] == FINANCE
    assert corrected["at"] == NOW.isoformat()
    assert corrected["details"] == {"field": "total", "before": "652.50", "after": "625.50"}
    [approved] = [e for e in trail["entries"] if e["kind"] == "approved"]
    assert (approved["actor"], approved["at"]) == (FINANCE, NOW.isoformat())
    filed, converted = trail["entries"][-3:-1]
    assert filed["actor"] == FINANCE
    assert filed["details"]["file_name"] == "2026-08_Slack_625.50-USD.pdf"
    assert filed["details"]["pending"] is False
    assert converted["details"] == {"currency": "USD", "rate": "95.34", "rate_date": "2026-08-03"}
    assert trail["state"] == "collected"
    assert trail["fields"]["total"] == "625.50"


def test_approval_without_changes_shows_no_correction(
    collection: Collection, dashboard: TestClient
) -> None:
    hold_slack(collection)

    approve(dashboard, slack_email())

    assert "corrected" not in kinds(history(dashboard))
    assert "approved" in kinds(history(dashboard))


def test_rejection_shows_who_rejected_it(collection: Collection, dashboard: TestClient) -> None:
    email = hold_slack(collection)

    assert dashboard.post(action_path(email, "reject")).status_code == 200

    trail = history(dashboard)
    assert trail["state"] == "rejected"
    [rejected] = [e for e in trail["entries"] if e["kind"] == "rejected"]
    assert (rejected["actor"], rejected["at"]) == (FINANCE, NOW.isoformat())
    assert trail["file_url"] is None


def test_removal_of_the_pending_copy_after_approval_is_a_step(
    collection: Collection, dashboard: TestClient
) -> None:
    hold_slack(collection)

    approve(dashboard, slack_email())

    [removed] = [e for e in history(dashboard)["entries"] if e["kind"] == "pending_copy_removed"]
    assert (removed["actor"], removed["at"]) == (FINANCE, NOW.isoformat())
    assert removed["details"] == {"file_name": PENDING_NAME}


def test_removal_of_the_pending_copy_after_rejection_is_a_step(
    collection: Collection, dashboard: TestClient
) -> None:
    email = hold_slack(collection)

    dashboard.post(action_path(email, "reject"))

    trail = history(dashboard)
    assert kinds(trail)[-2:] == ["rejected", "pending_copy_removed"]


def test_pending_copy_that_could_not_be_removed_is_a_step_with_the_reason(
    collection: Collection, tmp_path: Path
) -> None:
    drive = FakeDriveArchive()
    email = hold_slack_in_drive(collection, drive)
    drive.removable = False
    rates = FakeExchangeRates({"USD": Decimal("95.34")})
    with open_dashboard(tmp_path, rates, [NOW], drive) as client:
        sign_in(client)

        approve(client, email)

        trail = history(client)
    assert "pending_copy_removed" not in kinds(trail)
    [left] = [e for e in trail["entries"] if e["kind"] == "pending_copy_not_removed"]
    assert left["actor"] == FINANCE
    assert left["details"] == {
        "file_name": PENDING_NAME,
        "reason": f"The copy of {PENDING_NAME} in the pending folder could not be removed "
        "(Drive could not be reached). The approval stands; remove the copy by hand.",
    }


def test_unknown_document_is_not_found(dashboard: TestClient) -> None:
    assert dashboard.get(f"/api/billing-documents/{'0' * 64}/trail").status_code == 404


def test_malformed_content_hash_is_refused(dashboard: TestClient) -> None:
    assert dashboard.get("/api/billing-documents/not-a-hash/trail").status_code == 422


def test_history_needs_a_signed_in_person(tmp_path: Path) -> None:
    rates = FakeExchangeRates({})
    with open_dashboard(tmp_path, rates, [NOW]) as client:
        assert client.get(TRAIL).status_code == 401
