"""The Review screen's API, called the way the dashboard calls it.

Documents are put into the held state by running a collection with a reader that is
unsure of what it read, as a real run would hold them.
"""

import json
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from google.oauth2.credentials import Credentials
from support import AUGUST, ENGINEERING, JULY, OPS, Collection, invoice_email
from test_api import FINANCE, sign_in
from test_cli import OWNER, store_owner_sign_in
from test_run_checks import SLACK, SLACK_PDF, august, slack_email

from invoice_collector.api.app import create_app
from invoice_collector.api.identity import FakeIdentityVerifier
from invoice_collector.api.serve import main as serve_dashboard
from invoice_collector.api.settings import Settings
from invoice_collector.archive import BothArchives, LocalArchive
from invoice_collector.domain import Attachment, Email, EmailState, InvoiceFormat
from invoice_collector.exchange_rates import FakeExchangeRates
from invoice_collector.ledger import Ledger
from invoice_collector.rule_extractor import READ_BY_RULES

NOW = datetime(2026, 9, 29, 10, 30, tzinfo=UTC)
UNSURE = replace(SLACK, confidence="low", doubts="the total is smudged")
STATED_TOTAL = "Your invoice is attached. Total due: $625.50"
PENDING_LINK = "archive/2026-08/pending/2026-08_Slack_652.50-USD.pdf"
REVIEW = "/api/months/2026-08/review"


@pytest.fixture
def rates() -> FakeExchangeRates:
    return FakeExchangeRates({"USD": Decimal("95.34"), "EUR": Decimal("110.38")})


@pytest.fixture
def clock() -> list[datetime]:
    """The time the dashboard reads. A test moves it on by replacing the one entry."""
    return [NOW]


class FakeDriveArchive:
    """The owner account's Drive, keeping what is saved in memory."""

    def __init__(self, *, reachable: bool = True) -> None:
        self.reachable = reachable
        # Whether a copy can be removed, while saving still works.
        self.removable = True
        self.saved: list[tuple[str, str, bytes]] = []
        self.files: dict[tuple[str, str], bytes] = {}

    def save(self, folder: str, filename: str, pdf: bytes) -> str:
        if not self.reachable:
            raise ConnectionError("Drive could not be reached")
        self.saved.append((folder, filename, pdf))
        self.files[(folder, filename)] = pdf
        return f"https://drive.example/{folder}/{filename}"

    def remove(self, folder: str, filename: str, pdf: bytes) -> None:
        if not (self.reachable and self.removable):
            raise ConnectionError("Drive could not be reached")
        if self.files.get((folder, filename)) == pdf:
            del self.files[(folder, filename)]


def open_dashboard(
    tmp_path: Path,
    rates: FakeExchangeRates,
    clock: list[datetime],
    drive_archive: FakeDriveArchive | None = None,
) -> TestClient:
    ledger_path = tmp_path / "ledger.sqlite"
    settings = Settings(
        session_secret="a-secret-only-for-tests",
        allowlist=frozenset({FINANCE}),
        ledger_path=ledger_path,
    )
    verifier = FakeIdentityVerifier({"code-finance": FINANCE})
    app = create_app(
        settings,
        lambda: Ledger(ledger_path),
        verifier,
        exchange_rates=rates,
        drive_archive=drive_archive,
        now=lambda: clock[0],
    )
    return TestClient(app, base_url="http://localhost:8000", follow_redirects=False)


@pytest.fixture
def app_client(
    tmp_path: Path, rates: FakeExchangeRates, clock: list[datetime]
) -> Iterator[TestClient]:
    with open_dashboard(tmp_path, rates, clock) as client:
        yield client


@pytest.fixture
def dashboard(app_client: TestClient) -> TestClient:
    sign_in(app_client)
    return app_client


def doubted_slack() -> Email:
    """A Slack invoice whose email states a different total from the document."""
    return slack_email(text_body=STATED_TOTAL)


def hold_slack(collection: Collection) -> Email:
    """Runs August with a reader unsure of the Slack invoice, so it is held for review."""
    collection.answers[SLACK_PDF] = UNSURE
    collection.expect("Slack", usual="640.00")
    email = doubted_slack()
    result = collection.run([email])
    assert [p.extraction.vendor for p in result.pending] == ["Slack"]
    return email


def queue(dashboard: TestClient) -> list[dict[str, Any]]:
    response = dashboard.get(REVIEW)
    assert response.status_code == 200
    return response.json()["items"]


def action_path(email: Email, action: str) -> str:
    return f"{REVIEW}/{email.source_account}/{email.message_id}/{action}"


def fields_of(item: dict[str, Any], **changes: str) -> dict[str, Any]:
    """The fields of each held document of an item as extracted, with changes to all."""
    return {
        "documents": [
            {
                "content_hash": document["content_hash"],
                "vendor": document["vendor"],
                "invoice_date": document["invoice_date"],
                "total": document["total"],
                "currency": document["currency"],
                "document_type": document["document_type"],
                **changes,
            }
            for document in item["documents"]
        ]
    }


def approve(dashboard: TestClient, email: Email, **changes: str) -> Any:
    [item] = [i for i in queue(dashboard) if i["message_id"] == email.message_id]
    return dashboard.post(action_path(email, "approve"), json=fields_of(item, **changes))


def summary_rows(dashboard: TestClient) -> list[dict[str, Any]]:
    return dashboard.get("/api/months/2026-08/summary").json()["rows"]


def state_of(collection: Collection, email: Email) -> tuple[EmailState, str | None]:
    [examined] = [
        e for e in collection.ledger.examined_emails(AUGUST) if e.message_id == email.message_id
    ]
    return examined.state, examined.reason


def pending_folder(collection: Collection) -> list[str]:
    folder = collection.tmp_path / "archive" / "2026-08" / "pending"
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


def corrections(collection: Collection) -> list[dict[str, Any]]:
    path = collection.tmp_path / "corrections.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text("utf-8").splitlines()]


# The review queue


def test_review_queue_lists_held_documents_with_their_doubts_and_the_usual_amount(
    collection: Collection, dashboard: TestClient
) -> None:
    email = hold_slack(collection)

    [item] = queue(dashboard)

    assert item["source_account"] == ENGINEERING
    assert item["message_id"] == email.message_id
    assert item["sender"] == email.sender
    assert item["subject"] == email.subject
    assert item["received_at"] == email.received_at.isoformat()
    assert item["invoice_format"] == "attachment"
    assert item["portal_link"] is None
    assert item["needs_manual_download"] is False
    [document] = item["documents"]
    assert {k: document[k] for k in ("vendor", "invoice_date", "total", "currency")} == {
        "vendor": "Slack",
        "invoice_date": "2026-08-03",
        "total": "652.50",
        "currency": "USD",
    }
    assert document["document_type"] == "invoice"
    assert document["doubts"] == [
        {"field": None, "reason": "the reader was unsure: the total is smudged"},
        {"field": "total", "reason": "the email says 625.50 and the document says 652.50"},
    ]
    assert document["read_again"] is False
    assert document["file_name"] == "2026-08_Slack_652.50-USD.pdf"
    assert document["file_url"] == (
        f"{REVIEW}/billing-documents/{document['content_hash']}/2026-08_Slack_652.50-USD.pdf"
    )
    assert (document["usual_amount"], document["usual_currency"]) == ("640.00", "USD")
    assert document["source_accounts"] == [ENGINEERING]


def test_a_document_read_by_rules_says_so_and_why_it_is_held(
    collection: Collection, dashboard: TestClient
) -> None:
    by_rules = replace(SLACK, confidence="low", doubts=READ_BY_RULES, by="rules")
    collection.answers[SLACK_PDF] = by_rules
    collection.expect("Slack", usual="652.50")
    collection.run([slack_email()])

    [item] = queue(dashboard)

    [document] = item["documents"]
    assert document["read_by"] == "rules"
    assert document["doubts"] == [
        {"field": None, "reason": f"the reader was unsure: {READ_BY_RULES}"},
    ]


def test_a_document_read_by_a_model_names_it(collection: Collection, dashboard: TestClient) -> None:
    collection.answers[SLACK_PDF] = replace(UNSURE, by="claude-haiku-4-5")
    collection.run([doubted_slack()])

    [item] = queue(dashboard)

    assert item["documents"][0]["read_by"] == "claude-haiku-4-5"


def test_usual_amount_comes_from_earlier_months_for_a_vendor_on_no_list(
    collection: Collection, dashboard: TestClient
) -> None:
    july_pdf = b"%PDF-1.7 slack july"
    collection.answers[july_pdf] = replace(SLACK, invoice_date=SLACK.invoice_date.replace(month=7))
    july = invoice_email("Slack", july_pdf, received=datetime(2026, 7, 3, 9, 0, tzinfo=UTC))
    collection.run([july], JULY)
    collection.answers[SLACK_PDF] = UNSURE
    collection.run([doubted_slack()])

    [item] = queue(dashboard)

    [document] = item["documents"]
    assert (document["usual_amount"], document["usual_currency"]) == ("652.50", "USD")


def test_same_document_in_two_source_accounts_is_one_item(
    collection: Collection, dashboard: TestClient
) -> None:
    collection.answers[SLACK_PDF] = UNSURE
    copy = invoice_email("Slack", SLACK_PDF, received=august(3), account=OPS)
    collection.run([doubted_slack(), copy])

    [item] = queue(dashboard)

    assert item["documents"][0]["source_accounts"] == [ENGINEERING, OPS]
    assert item["message_ids"] == [doubted_slack().message_id, copy.message_id]


def test_email_needing_a_manual_download_is_listed_and_cannot_be_approved(
    collection: Collection, dashboard: TestClient
) -> None:
    zoom = Email(
        source_account=ENGINEERING,
        message_id="m-zoom",
        sender="Zoom <billing@zoom.example>",
        subject="Your Zoom invoice is ready",
        received_at=august(12),
    )
    collection.ledger.record(
        AUGUST,
        zoom,
        EmailState.NEEDS_REVIEW,
        reason="manual download needed",
        invoice_format=InvoiceFormat.PORTAL_LINK,
        portal_link="https://zoom.example/billing/invoices/889",
    )

    [item] = queue(dashboard)

    assert item["needs_manual_download"] is True
    assert item["portal_link"] == "https://zoom.example/billing/invoices/889"
    assert item["reason"] == "manual download needed"
    assert item["documents"] == []
    response = dashboard.post(action_path(zoom, "approve"), json={"documents": []})
    assert response.status_code == 409
    assert state_of(collection, zoom)[0] is EmailState.NEEDS_REVIEW


def test_queue_is_empty_when_nothing_is_held(dashboard: TestClient) -> None:
    assert queue(dashboard) == []


# The document


def test_pdf_of_a_held_document_is_served(collection: Collection, dashboard: TestClient) -> None:
    hold_slack(collection)
    [item] = queue(dashboard)

    response = dashboard.get(item["documents"][0]["file_url"])

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content == SLACK_PDF


def test_file_the_ledger_does_not_name_is_not_served(
    collection: Collection, dashboard: TestClient
) -> None:
    hold_slack(collection)
    (collection.tmp_path / "archive" / "2026-08" / "pending" / "stray.pdf").write_bytes(b"%PDF")

    [item] = dashboard.get(REVIEW).json()["items"]
    digest = item["documents"][0]["content_hash"]
    assert dashboard.get(f"{REVIEW}/billing-documents/{digest}/stray.pdf").status_code == 404
    assert dashboard.get(f"{REVIEW}/billing-documents/{digest}/ledger.sqlite").status_code == 404
    assert dashboard.get(f"{REVIEW}/billing-documents/nobody/stray.pdf").status_code == 404
    other_month = (
        f"/api/months/2026-07/review/billing-documents/{digest}/2026-08_Slack_652.50-USD.pdf"
    )
    assert dashboard.get(other_month).status_code == 404


# Approving


def test_approving_as_extracted_files_the_document_and_reports_it(
    collection: Collection, dashboard: TestClient
) -> None:
    email = hold_slack(collection)

    response = approve(dashboard, email)

    assert response.status_code == 200
    assert state_of(collection, email) == (EmailState.COLLECTED, None)
    assert collection.saved_files() == ["2026-08_Slack_652.50-USD.pdf"]
    assert pending_folder(collection) == []


def test_approving_with_a_corrected_total_files_it_under_the_corrected_name(
    collection: Collection, dashboard: TestClient
) -> None:
    email = hold_slack(collection)

    response = approve(dashboard, email, total="625.50")

    assert response.status_code == 200
    assert collection.saved_files() == ["2026-08_Slack_625.50-USD.pdf"]
    filed = collection.tmp_path / "archive" / "2026-08" / "2026-08_Slack_625.50-USD.pdf"
    assert filed.read_bytes() == SLACK_PDF
    assert pending_folder(collection) == []


def test_approved_document_appears_in_the_summary_and_leaves_the_queue(
    collection: Collection, dashboard: TestClient
) -> None:
    email = hold_slack(collection)

    approve(dashboard, email, total="625.50")

    [row] = summary_rows(dashboard)
    assert (row["vendor"], row["amount"], row["currency"]) == ("Slack", "625.50", "USD")
    assert (row["inr_rate"], row["amount_inr"]) == ("95.34", "59635.17")
    assert row["file_name"] == "2026-08_Slack_625.50-USD.pdf"
    assert dashboard.get(row["file_url"]).content == SLACK_PDF
    assert queue(dashboard) == []


def test_name_clash_with_a_different_document_gets_a_numbered_name(
    collection: Collection, dashboard: TestClient
) -> None:
    email = hold_slack(collection)
    month_folder = collection.tmp_path / "archive" / "2026-08"
    (month_folder / "2026-08_Slack_625.50-USD.pdf").write_bytes(b"%PDF-1.7 another document")

    approve(dashboard, email, total="625.50")

    assert "2026-08_Slack_625.50-USD_2.pdf" in collection.saved_files()
    assert (month_folder / "2026-08_Slack_625.50-USD.pdf").read_bytes() == (
        b"%PDF-1.7 another document"
    )


def test_credit_note_is_stored_with_a_negative_total(
    collection: Collection, dashboard: TestClient
) -> None:
    email = hold_slack(collection)

    response = approve(dashboard, email, document_type="credit_note", total="45.00")

    assert response.status_code == 200
    [row] = summary_rows(dashboard)
    assert (row["document_type"], row["amount"]) == ("credit_note", "-45.00")
    assert collection.saved_files() == ["2026-08_Slack_-45.00-USD.pdf"]


def test_currency_is_stored_in_capitals(collection: Collection, dashboard: TestClient) -> None:
    email = hold_slack(collection)

    approve(dashboard, email, currency="eur")

    [row] = summary_rows(dashboard)
    assert (row["currency"], row["inr_rate"]) == ("EUR", "110.38")


def test_missing_exchange_rate_does_not_stop_approval(
    collection: Collection, dashboard: TestClient
) -> None:
    email = hold_slack(collection)

    response = approve(dashboard, email, currency="GBP")

    assert response.status_code == 200
    [row] = summary_rows(dashboard)
    assert (row["currency"], row["amount_inr"]) == ("GBP", None)


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("vendor", "  ", "The vendor must not be blank"),
        ("invoice_date", "2026-08-32", "The invoice date must be a real date, as YYYY-MM-DD"),
        ("invoice_date", "3 August", "The invoice date must be a real date, as YYYY-MM-DD"),
        ("total", "six hundred", "The total must be a number, such as 652.50"),
        ("total", "Infinity", "The total must be a number, such as 652.50"),
        ("total", "NaN", "The total must be a number, such as 652.50"),
        ("currency", "US", "The currency must be three letters, such as USD"),
        ("currency", "U$D", "The currency must be three letters, such as USD"),
        ("currency", "ÜSD", "The currency must be three letters, such as USD"),
        ("document_type", "bill", "The document type must be invoice, receipt or credit_note"),
    ],
)
def test_approval_that_breaks_a_rule_changes_nothing(
    collection: Collection, dashboard: TestClient, field: str, value: str, reason: str
) -> None:
    email = hold_slack(collection)
    [item] = queue(dashboard)

    response = approve(dashboard, email, **{field: value})

    assert response.status_code == 422
    assert response.json()["detail"]["problems"] == [
        {"content_hash": item["documents"][0]["content_hash"], "field": field, "reason": reason}
    ]
    assert queue(dashboard) == [item]
    assert state_of(collection, email)[0] is EmailState.NEEDS_REVIEW
    assert pending_folder(collection) == ["2026-08_Slack_652.50-USD.pdf"]
    assert collection.saved_files() == ["pending"]
    assert summary_rows(dashboard) == []
    assert dashboard.get(f"{REVIEW}/history").json() == []


def test_invoice_date_in_another_month_is_refused_naming_that_month(
    collection: Collection, dashboard: TestClient
) -> None:
    email = hold_slack(collection)

    response = approve(dashboard, email, invoice_date="2026-09-02")

    assert response.status_code == 422
    [problem] = response.json()["detail"]["problems"]
    assert problem["field"] == "invoice_date"
    assert problem["reason"] == (
        "The invoice date 2026-09-02 belongs to collection month 2026-09, not 2026-08"
    )
    assert state_of(collection, email)[0] is EmailState.NEEDS_REVIEW


def test_every_held_document_of_the_email_must_be_confirmed(
    collection: Collection, dashboard: TestClient
) -> None:
    email = hold_slack(collection)

    response = dashboard.post(action_path(email, "approve"), json={"documents": []})

    assert response.status_code == 422
    assert state_of(collection, email)[0] is EmailState.NEEDS_REVIEW


def test_approving_an_email_that_is_not_held_is_refused(
    collection: Collection, dashboard: TestClient
) -> None:
    collection.answers[SLACK_PDF] = SLACK
    email = slack_email()
    collection.run([email])

    response = dashboard.post(action_path(email, "approve"), json={"documents": []})

    assert response.status_code == 404


def test_email_with_two_held_documents_is_approved_together(
    collection: Collection, dashboard: TestClient
) -> None:
    second_pdf = b"%PDF-1.7 slack august usage"
    collection.answers[SLACK_PDF] = UNSURE
    collection.answers[second_pdf] = replace(UNSURE, total=Decimal("12.00"))
    email = replace(
        slack_email(),
        attachments=(
            Attachment("invoice.pdf", "application/pdf", SLACK_PDF),
            Attachment("usage.pdf", "application/pdf", second_pdf),
        ),
    )
    collection.run([email])
    [item] = queue(dashboard)
    assert len(item["documents"]) == 2

    response = approve(dashboard, email)

    assert response.status_code == 200
    assert sorted(row["amount"] for row in summary_rows(dashboard)) == ["12.00", "652.50"]
    assert pending_folder(collection) == []
    assert queue(dashboard) == []


def test_one_bad_document_of_two_changes_nothing(
    collection: Collection, dashboard: TestClient
) -> None:
    second_pdf = b"%PDF-1.7 slack august usage"
    collection.answers[SLACK_PDF] = UNSURE
    collection.answers[second_pdf] = replace(UNSURE, total=Decimal("12.00"))
    email = replace(
        slack_email(),
        attachments=(
            Attachment("invoice.pdf", "application/pdf", SLACK_PDF),
            Attachment("usage.pdf", "application/pdf", second_pdf),
        ),
    )
    collection.run([email])
    [item] = queue(dashboard)
    body = fields_of(item)
    body["documents"][1]["currency"] = "dollars"

    response = dashboard.post(action_path(email, "approve"), json=body)

    assert response.status_code == 422
    [problem] = response.json()["detail"]["problems"]
    assert (problem["content_hash"], problem["field"]) == (
        item["documents"][1]["content_hash"],
        "currency",
    )
    assert summary_rows(dashboard) == []
    assert len(pending_folder(collection)) == 2


def test_approving_one_copy_approves_the_document_in_every_source_account(
    collection: Collection, dashboard: TestClient
) -> None:
    collection.answers[SLACK_PDF] = UNSURE
    copy = invoice_email("Slack", SLACK_PDF, received=august(3), account=OPS)
    email = doubted_slack()
    collection.run([email, copy])

    approve(dashboard, email)

    [row] = summary_rows(dashboard)
    assert row["source_account"] == f"{ENGINEERING}; {OPS}"
    assert state_of(collection, copy)[0] is EmailState.COLLECTED
    assert queue(dashboard) == []


# Not a billing document


def test_rejecting_deletes_the_pending_pdf_and_records_the_email_as_skipped(
    collection: Collection, dashboard: TestClient
) -> None:
    email = hold_slack(collection)

    response = dashboard.post(action_path(email, "reject"))

    assert response.status_code == 200
    assert state_of(collection, email) == (
        EmailState.SKIPPED,
        "a person judged it not to be a billing document",
    )
    assert pending_folder(collection) == []
    assert collection.saved_files() == []
    assert summary_rows(dashboard) == []
    assert queue(dashboard) == []


# Running the month again


def test_approved_correction_survives_a_second_run_of_the_month(
    collection: Collection, dashboard: TestClient
) -> None:
    email = hold_slack(collection)
    approve(dashboard, email, total="625.50")

    result = collection.run([email])

    assert [(row.vendor, row.total) for row in result.summary] == [("Slack", Decimal("625.50"))]
    assert result.pending == []
    assert collection.saved_files() == ["2026-08_Slack_625.50-USD.pdf"]
    assert pending_folder(collection) == []


def month_gaps(dashboard: TestClient) -> list[dict[str, Any]]:
    return dashboard.get("/api/months/2026-08/summary").json()["gaps"]


def test_gap_says_a_document_is_held_until_a_person_approves_it(
    collection: Collection, dashboard: TestClient
) -> None:
    email = hold_slack(collection)
    [gap] = month_gaps(dashboard)
    assert gap["explanation"] == (
        "held for review: the reader was unsure: the total is smudged, and 1 more"
    )

    approve(dashboard, email)

    assert month_gaps(dashboard) == []


def test_gap_is_plainly_missing_once_a_person_rejects_the_held_document(
    collection: Collection, dashboard: TestClient
) -> None:
    email = hold_slack(collection)

    dashboard.post(action_path(email, "reject"))

    [gap] = month_gaps(dashboard)
    assert (gap["kind"], gap["explanation"]) == ("missing", None)


def test_vendor_a_person_confirmed_is_not_matched_again_by_a_second_run(
    collection: Collection, dashboard: TestClient
) -> None:
    email = hold_slack(collection)
    approve(dashboard, email, vendor="Slack Enterprise Grid")

    result = collection.run([email])

    assert [row.vendor for row in result.summary] == ["Slack Enterprise Grid"]
    [document] = collection.ledger.documents(AUGUST)
    assert document.vendor_as_read == "Slack"


def test_rejected_email_is_not_held_again_by_a_second_run(
    collection: Collection, dashboard: TestClient
) -> None:
    email = hold_slack(collection)
    dashboard.post(action_path(email, "reject"))

    result = collection.run([email])

    assert result.pending == []
    assert result.summary == []
    assert state_of(collection, email) == (
        EmailState.SKIPPED,
        "a person judged it not to be a billing document",
    )
    assert pending_folder(collection) == []


# The record of decisions


def test_history_records_who_decided_and_the_fields_before_and_after(
    collection: Collection, dashboard: TestClient
) -> None:
    email = hold_slack(collection)
    [item] = queue(dashboard)
    approve(dashboard, email, total="625.50")

    [decision] = dashboard.get(f"{REVIEW}/history").json()

    assert decision["action"] == "approved"
    assert decision["person"] == FINANCE
    assert decision["decided_at"] == NOW.isoformat()
    assert (decision["source_account"], decision["message_id"]) == (ENGINEERING, email.message_id)
    assert decision["subject"] == email.subject
    [document] = decision["documents"]
    assert document["content_hash"] == item["documents"][0]["content_hash"]
    assert document["before"]["total"] == "652.50"
    assert document["after"]["total"] == "625.50"
    assert document["changed_fields"] == ["total"]


def test_history_is_newest_first_and_records_rejections(
    collection: Collection, dashboard: TestClient, clock: list[datetime]
) -> None:
    figma_pdf = b"%PDF-1.7 figma"
    collection.answers[figma_pdf] = replace(UNSURE, vendor="Figma", total=Decimal("190.00"))
    figma = invoice_email("Figma", figma_pdf, received=august(21))
    email = hold_slack(collection)
    collection.run([email, figma])
    approve(dashboard, email)
    clock[0] = datetime(2026, 9, 29, 11, 0, tzinfo=UTC)
    dashboard.post(action_path(figma, "reject"))

    history = dashboard.get(f"{REVIEW}/history").json()

    assert [(d["action"], d["message_id"], d["person"]) for d in history] == [
        ("rejected", figma.message_id, FINANCE),
        ("approved", email.message_id, FINANCE),
    ]
    assert history[0]["documents"][0]["after"] is None
    assert history[0]["documents"][0]["before"]["vendor"] == "Figma"


# Corrections feed the golden dataset


def test_correction_is_appended_to_the_corrections_file(
    collection: Collection, dashboard: TestClient
) -> None:
    email = hold_slack(collection)
    [item] = queue(dashboard)

    approve(dashboard, email, total="625.50")

    [correction] = corrections(collection)
    assert correction["content_hash"] == item["documents"][0]["content_hash"]
    assert correction["extracted"]["total"] == "652.50"
    assert correction["confirmed"]["total"] == "625.50"
    assert correction["confirmed"]["vendor"] == "Slack"
    assert correction["doubts"] == item["documents"][0]["doubts"]
    assert (correction["person"], correction["corrected_at"]) == (FINANCE, NOW.isoformat())


def test_approval_without_changes_adds_no_correction(
    collection: Collection, dashboard: TestClient
) -> None:
    email = hold_slack(collection)

    approve(dashboard, email, total="652.5")

    assert corrections(collection) == []


def test_corrections_are_kept_beside_the_ledger_not_in_the_samples(
    collection: Collection, dashboard: TestClient
) -> None:
    email = hold_slack(collection)

    approve(dashboard, email, vendor="Slack Technologies")

    assert (collection.tmp_path / "corrections.jsonl").exists()


# Filing to the owner account's Drive


def test_approval_files_to_the_owner_accounts_drive_as_a_run_does(
    collection: Collection, rates: FakeExchangeRates, clock: list[datetime]
) -> None:
    email = hold_slack(collection)
    drive = FakeDriveArchive()
    with open_dashboard(collection.tmp_path, rates, clock, drive) as dashboard:
        sign_in(dashboard)

        response = approve(dashboard, email, total="625.50")

        assert response.status_code == 200
        name = "2026-08_Slack_625.50-USD.pdf"
        assert drive.saved == [("2026-08", name, SLACK_PDF)]
        assert collection.saved_files() == [name]
        assert pending_folder(collection) == []
        [row] = summary_rows(dashboard)
        assert row["file_url"] == f"https://drive.example/2026-08/{name}"


def test_approval_while_drive_cannot_be_reached_leaves_the_email_held(
    collection: Collection, rates: FakeExchangeRates, clock: list[datetime]
) -> None:
    email = hold_slack(collection)
    drive = FakeDriveArchive(reachable=False)
    with open_dashboard(collection.tmp_path, rates, clock, drive) as dashboard:
        sign_in(dashboard)

        response = approve(dashboard, email)

        assert response.status_code == 502
        assert "Nothing was changed" in response.json()["detail"]
        assert state_of(collection, email)[0] is EmailState.NEEDS_REVIEW
        assert pending_folder(collection) == ["2026-08_Slack_652.50-USD.pdf"]
        assert [i["message_id"] for i in queue(dashboard)] == [email.message_id]
        assert dashboard.get(f"{REVIEW}/history").json() == []

        drive.reachable = True
        assert approve(dashboard, email).status_code == 200
        assert state_of(collection, email) == (EmailState.COLLECTED, None)


def test_without_the_owner_accounts_drive_approval_files_locally_only(
    collection: Collection, dashboard: TestClient
) -> None:
    email = hold_slack(collection)

    approve(dashboard, email)

    [row] = summary_rows(dashboard)
    assert not row["file_url"].startswith("https://")
    assert dashboard.get(row["file_url"]).content == SLACK_PDF


PENDING_NAME = "2026-08_Slack_652.50-USD.pdf"


def hold_slack_in_drive(collection: Collection, drive: FakeDriveArchive) -> Email:
    """Holds the Slack invoice in a run that files to Drive as well, as one with an owner."""
    collection.archive = BothArchives(drive, LocalArchive(collection.tmp_path / "archive"))
    email = hold_slack(collection)
    assert ("2026-08/pending", PENDING_NAME) in drive.files
    return email


def test_approval_removes_the_pending_copy_from_drive(
    collection: Collection, rates: FakeExchangeRates, clock: list[datetime]
) -> None:
    drive = FakeDriveArchive()
    email = hold_slack_in_drive(collection, drive)
    with open_dashboard(collection.tmp_path, rates, clock, drive) as dashboard:
        sign_in(dashboard)

        response = approve(dashboard, email)

        assert response.status_code == 200
        assert response.json()["warnings"] == []
        assert list(drive.files) == [("2026-08", PENDING_NAME)]
        assert pending_folder(collection) == []


def test_rejection_removes_the_pending_copy_from_drive(
    collection: Collection, rates: FakeExchangeRates, clock: list[datetime]
) -> None:
    drive = FakeDriveArchive()
    email = hold_slack_in_drive(collection, drive)
    with open_dashboard(collection.tmp_path, rates, clock, drive) as dashboard:
        sign_in(dashboard)

        response = dashboard.post(action_path(email, "reject"))

        assert response.status_code == 200
        assert drive.files == {}
        assert pending_folder(collection) == []


def test_pending_copy_that_cannot_be_removed_is_reported_and_the_approval_stands(
    collection: Collection, rates: FakeExchangeRates, clock: list[datetime]
) -> None:
    drive = FakeDriveArchive()
    email = hold_slack_in_drive(collection, drive)
    drive.removable = False
    with open_dashboard(collection.tmp_path, rates, clock, drive) as dashboard:
        sign_in(dashboard)

        response = approve(dashboard, email)

        assert response.status_code == 200
        assert response.json()["warnings"] == [
            f"The copy of {PENDING_NAME} in the pending folder could not be removed "
            "(Drive could not be reached). The approval stands; remove the copy by hand."
        ]
        assert state_of(collection, email) == (EmailState.COLLECTED, None)
        assert ("2026-08/pending", PENDING_NAME) in drive.files
        assert queue(dashboard) == []


def test_pending_copy_that_cannot_be_removed_is_reported_and_the_rejection_stands(
    collection: Collection, rates: FakeExchangeRates, clock: list[datetime]
) -> None:
    drive = FakeDriveArchive()
    email = hold_slack_in_drive(collection, drive)
    drive.removable = False
    with open_dashboard(collection.tmp_path, rates, clock, drive) as dashboard:
        sign_in(dashboard)

        response = dashboard.post(action_path(email, "reject"))

        assert response.status_code == 200
        assert len(response.json()["warnings"]) == 1
        assert state_of(collection, email)[0] is EmailState.SKIPPED


def test_pending_copy_in_drive_left_by_a_dashboard_without_drive_is_reported(
    collection: Collection, dashboard: TestClient
) -> None:
    drive = FakeDriveArchive()
    email = hold_slack_in_drive(collection, drive)

    response = approve(dashboard, email)

    assert response.status_code == 200
    [warning] = response.json()["warnings"]
    assert "the dashboard has no owner account" in warning
    assert pending_folder(collection) == []


def dashboard_environment(tmp_path: Path) -> dict[str, str]:
    web_client = tmp_path / "web-client.json"
    web_client.write_text(
        '{"web": {"client_id": "made-up-id", "client_secret": "made-up-value"}}',
        encoding="utf-8",
    )
    return {
        "INVOICE_COLLECTOR_SESSION_SECRET": "a-secret-only-for-tests",
        "INVOICE_COLLECTOR_ALLOWLIST": FINANCE,
        "INVOICE_COLLECTOR_WEB_CLIENT_FILE": str(web_client),
        "INVOICE_COLLECTOR_TOKEN_DIR": str(tmp_path / "tokens"),
    }


def test_dashboard_command_with_an_owner_account_reaches_drive_only_to_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store_owner_sign_in(tmp_path / "tokens")
    signed_in: list[Credentials] = []
    served: list[FastAPI] = []

    def services(credentials: Credentials) -> tuple[Any, Any]:
        signed_in.append(credentials)
        return object(), object()

    exit_code = serve_dashboard(
        ["--ledger", str(tmp_path / "ledger.sqlite"), "--google-owner", OWNER],
        environment=dashboard_environment(tmp_path),
        google_services=services,
        serve=lambda app, host, port: served.append(app),
    )

    assert exit_code == 0
    assert len(served) == 1
    # The owner account is looked up each time a document is filed, not when it starts.
    assert signed_in == []
    assert "owner account" not in capsys.readouterr().err


def test_dashboard_command_starts_when_the_owner_account_is_not_signed_in(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    served: list[FastAPI] = []

    exit_code = serve_dashboard(
        ["--ledger", str(tmp_path / "ledger.sqlite"), "--google-owner", OWNER],
        environment=dashboard_environment(tmp_path),
        serve=lambda app, host, port: served.append(app),
    )

    said = capsys.readouterr().err
    assert exit_code == 0
    assert len(served) == 1
    assert f"Warning: The owner account {OWNER} is not signed in to Google Drive" in said
    assert "Connect it on the Source accounts screen as the owner account" in said
    assert "on this machine only" in said


# Signing in


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", REVIEW),
        ("GET", f"{REVIEW}/history"),
        ("GET", f"{REVIEW}/billing-documents/2026-08_Slack_652.50-USD.pdf"),
        ("POST", f"{REVIEW}/{ENGINEERING}/m-slack/approve"),
        ("POST", f"{REVIEW}/{ENGINEERING}/m-slack/reject"),
    ],
)
def test_review_routes_return_401_when_not_signed_in(
    app_client: TestClient, method: str, path: str
) -> None:
    assert app_client.request(method, path).status_code == 401
