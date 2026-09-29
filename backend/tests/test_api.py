"""Tests at the dashboard's API seam: call the API the way the dashboard does."""

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from invoice_collector.api.app import create_app
from invoice_collector.api.identity import FakeIdentityVerifier, GoogleIdentityVerifier, WebClient
from invoice_collector.api.serve import main
from invoice_collector.api.settings import Settings, SettingsError
from invoice_collector.archive import LocalArchive
from invoice_collector.domain import (
    BillingSignal,
    CollectionMonth,
    Email,
    EmailState,
    ExpectedVendor,
    Extraction,
    InvoiceFormat,
)
from invoice_collector.ledger import CollectedDocument, Ledger

AUGUST = CollectionMonth(2026, 8)
JULY = CollectionMonth(2026, 7)
ENGINEERING = "engineering@nyayalabs.example"
DESIGN = "design@nyayalabs.example"

FINANCE = "finance@nyayalabs.example"
STRANGER = "stranger@elsewhere.example"
FRONT_END = "http://localhost:5173"


@pytest.fixture
def ledger_path(tmp_path: Path) -> Path:
    return tmp_path / "out" / "ledger.sqlite"


@pytest.fixture
def ledger(ledger_path: Path) -> Iterator[Ledger]:
    ledger = Ledger(ledger_path)
    yield ledger
    ledger.close()


@pytest.fixture
def settings(ledger_path: Path) -> Settings:
    return Settings(
        session_secret="a-secret-only-for-tests",
        allowlist=frozenset({FINANCE}),
        ledger_path=ledger_path,
    )


@pytest.fixture
def verifier() -> FakeIdentityVerifier:
    return FakeIdentityVerifier({"code-finance": FINANCE, "code-stranger": STRANGER})


@pytest.fixture
def dashboard(
    settings: Settings, ledger_path: Path, verifier: FakeIdentityVerifier
) -> Iterator[TestClient]:
    app = create_app(settings, lambda: Ledger(ledger_path), verifier)
    with TestClient(app, base_url="http://localhost:8000", follow_redirects=False) as client:
        yield client


def begin_sign_in(dashboard: TestClient) -> str:
    """Presses the sign-in button and returns the state sent to Google."""
    response = dashboard.get("/auth/login")
    assert response.status_code in (302, 307)
    return parse_qs(urlparse(response.headers["location"]).query)["state"][0]


def sign_in(dashboard: TestClient, code: str = "code-finance") -> None:
    state = begin_sign_in(dashboard)
    dashboard.get("/auth/callback", params={"state": state, "code": code})


@pytest.mark.parametrize(
    "path",
    [
        "/api/me",
        "/api/months",
        "/api/months/2026-08/summary",
        "/api/months/2026-08/billing-documents/2026-08_Slack_652.50-USD.pdf",
        "/api/anything-else",
    ],
)
def test_api_routes_return_401_when_not_signed_in(dashboard: TestClient, path: str) -> None:
    assert dashboard.get(path).status_code == 401


def test_sign_in_sends_the_person_to_google_with_a_state(dashboard: TestClient) -> None:
    response = dashboard.get("/auth/login")

    location = urlparse(response.headers["location"])
    assert location.netloc == "accounts.google.example"
    assert len(parse_qs(location.query)["state"][0]) >= 32


def test_session_cookie_is_http_only(dashboard: TestClient) -> None:
    response = dashboard.get("/auth/login")

    assert "httponly" in response.headers["set-cookie"].lower()


def test_sign_in_succeeds_for_an_address_on_the_allowlist(dashboard: TestClient) -> None:
    state = begin_sign_in(dashboard)

    response = dashboard.get("/auth/callback", params={"state": state, "code": "code-finance"})

    assert response.status_code in (302, 307)
    assert response.headers["location"] == f"{FRONT_END}/"
    me = dashboard.get("/api/me")
    assert me.status_code == 200
    assert me.json() == {"email": FINANCE, "role": "administrator"}


def test_allowlist_ignores_letter_case(
    settings: Settings, ledger_path: Path, verifier: FakeIdentityVerifier
) -> None:
    verifier.emails["code-shouting"] = "Finance@NyayaLabs.example"
    app = create_app(settings, lambda: Ledger(ledger_path), verifier)
    with TestClient(app, base_url="http://localhost:8000", follow_redirects=False) as dashboard:
        sign_in(dashboard, "code-shouting")

        assert dashboard.get("/api/me").status_code == 200


def test_sign_in_is_refused_for_an_address_not_on_the_allowlist(dashboard: TestClient) -> None:
    state = begin_sign_in(dashboard)

    response = dashboard.get("/auth/callback", params={"state": state, "code": "code-stranger"})

    assert response.headers["location"] == f"{FRONT_END}/?sign_in=refused"
    assert dashboard.get("/api/me").status_code == 401


def test_sign_in_is_refused_when_google_does_not_verify_the_person(
    dashboard: TestClient,
) -> None:
    state = begin_sign_in(dashboard)

    response = dashboard.get("/auth/callback", params={"state": state, "code": "code-unknown"})

    assert response.headers["location"] == f"{FRONT_END}/?sign_in=failed"
    assert dashboard.get("/api/me").status_code == 401


def test_callback_with_a_wrong_state_is_refused(
    dashboard: TestClient, verifier: FakeIdentityVerifier
) -> None:
    begin_sign_in(dashboard)

    response = dashboard.get(
        "/auth/callback", params={"state": "not-the-state", "code": "code-finance"}
    )

    assert response.headers["location"] == f"{FRONT_END}/?sign_in=failed"
    assert dashboard.get("/api/me").status_code == 401
    assert verifier.codes_exchanged == []


def test_callback_without_a_sign_in_in_progress_is_refused(
    dashboard: TestClient, verifier: FakeIdentityVerifier
) -> None:
    dashboard.get("/auth/callback", params={"state": "", "code": "code-finance"})

    assert dashboard.get("/api/me").status_code == 401
    assert verifier.codes_exchanged == []


def test_a_state_cannot_be_used_twice(dashboard: TestClient) -> None:
    state = begin_sign_in(dashboard)
    dashboard.get("/auth/callback", params={"state": state, "code": "code-stranger"})

    dashboard.get("/auth/callback", params={"state": state, "code": "code-finance"})

    assert dashboard.get("/api/me").status_code == 401


def test_person_removed_from_the_allowlist_loses_access(
    ledger_path: Path, verifier: FakeIdentityVerifier
) -> None:
    def dashboard_allowing(*allowlist: str) -> TestClient:
        settings = Settings(
            session_secret="a-secret-only-for-tests",
            allowlist=frozenset(allowlist),
            ledger_path=ledger_path,
        )
        app = create_app(settings, lambda: Ledger(ledger_path), verifier)
        return TestClient(app, base_url="http://localhost:8000", follow_redirects=False)

    before = dashboard_allowing(FINANCE)
    sign_in(before)
    after = dashboard_allowing("someone-else@nyayalabs.example")
    after.cookies.update(before.cookies)

    assert before.get("/api/me").status_code == 200
    assert after.get("/api/me").status_code == 401


def test_sign_out_ends_the_session(dashboard: TestClient) -> None:
    sign_in(dashboard)

    response = dashboard.post("/auth/logout")

    assert response.status_code == 204
    assert dashboard.get("/api/me").status_code == 401


def test_only_the_front_end_origin_may_call_with_credentials(dashboard: TestClient) -> None:
    from_front_end = dashboard.get("/api/me", headers={"Origin": FRONT_END})
    from_elsewhere = dashboard.get("/api/me", headers={"Origin": "http://evil.example"})

    assert from_front_end.headers["access-control-allow-origin"] == FRONT_END
    assert from_front_end.headers["access-control-allow-credentials"] == "true"
    assert "access-control-allow-origin" not in from_elsewhere.headers


def test_app_refuses_to_start_without_a_session_secret(ledger_path: Path) -> None:
    with pytest.raises(SettingsError, match="INVOICE_COLLECTOR_SESSION_SECRET"):
        Settings.from_environment({"INVOICE_COLLECTOR_ALLOWLIST": FINANCE}, ledger_path=ledger_path)


def test_app_refuses_to_start_with_an_empty_session_secret(
    ledger_path: Path, verifier: FakeIdentityVerifier
) -> None:
    settings = Settings(
        session_secret="  ", allowlist=frozenset({FINANCE}), ledger_path=ledger_path
    )

    with pytest.raises(SettingsError, match="INVOICE_COLLECTOR_SESSION_SECRET"):
        create_app(settings, lambda: Ledger(ledger_path), verifier)


def test_dashboard_command_refuses_to_start_without_a_session_secret(
    ledger_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(
        ["--ledger", str(ledger_path)], environment={"INVOICE_COLLECTOR_ALLOWLIST": FINANCE}
    )

    assert exit_code != 0
    assert "INVOICE_COLLECTOR_SESSION_SECRET is not set" in capsys.readouterr().err


def test_dashboard_command_refuses_to_start_without_a_web_client_file(
    tmp_path: Path, ledger_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(
        ["--ledger", str(ledger_path)],
        environment={
            "INVOICE_COLLECTOR_SESSION_SECRET": "a-secret-only-for-tests",
            "INVOICE_COLLECTOR_WEB_CLIENT_FILE": str(tmp_path / "absent.json"),
        },
    )

    assert exit_code != 0
    assert "absent.json does not exist" in capsys.readouterr().err


def test_person_is_sent_to_google_for_an_authorization_code(tmp_path: Path) -> None:
    web_client_file = tmp_path / "web-client.json"
    web_client_file.write_text(
        '{"web": {"client_id": "made-up-id", "client_secret": "made-up-value"}}',
        encoding="utf-8",
    )
    client = WebClient.read(web_client_file)
    verifier = GoogleIdentityVerifier(client, "http://localhost:8000/auth/callback")

    location = urlparse(verifier.authorization_url("the-state"))

    assert location.netloc == "accounts.google.com"
    assert parse_qs(location.query) == {
        "client_id": ["made-up-id"],
        "redirect_uri": ["http://localhost:8000/auth/callback"],
        "response_type": ["code"],
        "scope": ["openid email"],
        "state": ["the-state"],
        "prompt": ["select_account"],
    }
    assert "made-up-value" not in repr(client)


def test_settings_are_read_from_the_environment(ledger_path: Path) -> None:
    settings = Settings.from_environment(
        {
            "INVOICE_COLLECTOR_SESSION_SECRET": "a-secret-only-for-tests",
            "INVOICE_COLLECTOR_ALLOWLIST": f" {FINANCE} , Ops@NyayaLabs.example ,",
        },
        ledger_path=ledger_path,
    )

    assert settings.allowlist == frozenset({FINANCE, "ops@nyayalabs.example"})
    assert settings.web_client_file.as_posix().endswith("credentials/web-client.json")


def email(
    message_id: str, subject: str, *, account: str = ENGINEERING, day: int = 3, month: int = 8
) -> Email:
    return Email(
        source_account=account,
        message_id=message_id,
        sender="Billing <billing@vendor.example>",
        subject=subject,
        received_at=datetime(2026, month, day, 9, 0, tzinfo=UTC),
    )


def collected(ledger_path: Path, extraction: Extraction, pdf: bytes) -> CollectedDocument:
    """Files the PDF the way a run does, beside the ledger."""
    archive = LocalArchive(ledger_path.parent / "archive")
    name = f"2026-08_{extraction.vendor}_{extraction.total:.2f}-{extraction.currency}.pdf"
    return CollectedDocument(
        content_hash=f"hash-of-{name}",
        extraction=extraction,
        file_link=archive.save("2026-08", name, pdf),
        inr_rate=Decimal("95.34") if extraction.currency == "USD" else None,
    )


def record_august(ledger: Ledger, ledger_path: Path) -> None:
    """One month holding every kind of outcome a run can record."""
    slack = Extraction("invoice", "Slack", date(2026, 8, 3), Decimal("652.50"), "USD")
    notion = Extraction("receipt", "Notion", date(2026, 8, 9), Decimal("221.4"), "EUR")
    figma = Extraction("credit_note", "Figma", date(2026, 8, 21), Decimal("-40.00"), "USD")
    ledger.record(
        AUGUST,
        email("m-slack", "Your Slack invoice is available", day=3),
        EmailState.COLLECTED,
        invoice_format=InvoiceFormat.ATTACHMENT,
        documents=(collected(ledger_path, slack, b"%PDF-1.7 slack invoice"),),
    )
    ledger.record(
        AUGUST,
        email("m-notion", "Receipt from Notion", account=DESIGN, day=9),
        EmailState.COLLECTED,
        invoice_format=InvoiceFormat.BODY,
        documents=(collected(ledger_path, notion, b"%PDF-1.7 notion receipt"),),
    )
    ledger.record(
        AUGUST,
        email("m-figma", "Credit note from Figma", account=DESIGN, day=21),
        EmailState.COLLECTED,
        invoice_format=InvoiceFormat.ATTACHMENT,
        documents=(collected(ledger_path, figma, b"%PDF-1.7 figma credit note"),),
    )
    ledger.record(
        AUGUST,
        email("m-zoom", "Your Zoom invoice is ready", day=12),
        EmailState.NEEDS_REVIEW,
        reason="manual download needed",
        invoice_format=InvoiceFormat.PORTAL_LINK,
        portal_link="https://zoom.example/billing/invoices/889",
    )
    ledger.record(
        AUGUST,
        email("m-news", "What is new in Slack", day=10),
        EmailState.SKIPPED,
        reason="not a billing email",
    )
    ledger.record(
        AUGUST,
        email("m-github", "Your GitHub invoice", day=14),
        EmailState.FAILED,
        reason="the PDF could not be read",
        invoice_format=InvoiceFormat.ATTACHMENT,
    )
    linear = email("m-linear", "Payment failed for Linear", day=16)
    ledger.record(
        AUGUST,
        linear,
        EmailState.SKIPPED,
        reason="billing signal: payment failed",
        signal=BillingSignal(
            kind="payment_failed",
            vendor="Linear",
            source_account=linear.source_account,
            message_id=linear.message_id,
            subject=linear.subject,
            received_at=linear.received_at,
        ),
    )


def test_months_lists_the_collection_months_in_the_ledger_newest_first(
    dashboard: TestClient, ledger: Ledger
) -> None:
    ledger.record(JULY, email("m-july", "July", month=7), EmailState.SKIPPED, reason="x")
    ledger.record(AUGUST, email("m-august-1", "August"), EmailState.SKIPPED, reason="x")
    ledger.record(AUGUST, email("m-august-2", "August"), EmailState.SKIPPED, reason="x")
    sign_in(dashboard)

    response = dashboard.get("/api/months")

    assert response.status_code == 200
    assert response.json() == {"months": ["2026-08", "2026-07"]}


def test_months_is_empty_before_the_first_run(dashboard: TestClient) -> None:
    sign_in(dashboard)

    response = dashboard.get("/api/months")

    assert response.json() == {"months": []}


def test_listing_months_does_not_create_a_ledger(dashboard: TestClient, ledger_path: Path) -> None:
    sign_in(dashboard)

    dashboard.get("/api/months")

    assert not ledger_path.exists()


def test_summary_for_a_month_with_every_kind_of_outcome(
    dashboard: TestClient, ledger: Ledger, ledger_path: Path
) -> None:
    record_august(ledger, ledger_path)
    sign_in(dashboard)

    response = dashboard.get("/api/months/2026-08/summary")

    assert response.status_code == 200
    summary = response.json()
    assert summary["month"] == "2026-08"
    assert summary["rows"] == [
        {
            "vendor": "Slack",
            "document_type": "invoice",
            "date": "2026-08-03",
            "amount": "652.50",
            "currency": "USD",
            "source_account": ENGINEERING,
            "file_name": "2026-08_Slack_652.50-USD.pdf",
            "file_url": "/api/months/2026-08/billing-documents/2026-08_Slack_652.50-USD.pdf",
            "amount_inr": "62209.35",
            "inr_rate": "95.34",
            "notes": "",
        },
        {
            "vendor": "Notion",
            "document_type": "receipt",
            "date": "2026-08-09",
            "amount": "221.40",
            "currency": "EUR",
            "source_account": DESIGN,
            "file_name": "2026-08_Notion_221.40-EUR.pdf",
            "file_url": "/api/months/2026-08/billing-documents/2026-08_Notion_221.40-EUR.pdf",
            "amount_inr": None,
            "inr_rate": None,
            "notes": "",
        },
        {
            "vendor": "Figma",
            "document_type": "credit_note",
            "date": "2026-08-21",
            "amount": "-40.00",
            "currency": "USD",
            "source_account": DESIGN,
            "file_name": "2026-08_Figma_-40.00-USD.pdf",
            "file_url": "/api/months/2026-08/billing-documents/2026-08_Figma_-40.00-USD.pdf",
            "amount_inr": "-3813.60",
            "inr_rate": "95.34",
            "notes": "",
        },
    ]
    assert summary["totals"] == [
        {"currency": "EUR", "amount": "221.40"},
        {"currency": "USD", "amount": "612.50"},
    ]
    assert (summary["total_inr"], summary["rows_without_rupees"]) == ("58395.75", 1)
    assert summary["counts"] == {"collected": 3, "needs_review": 1, "skipped": 2, "failed": 1}
    assert summary["needs_review"] == [
        {
            "source_account": ENGINEERING,
            "message_id": "m-zoom",
            "subject": "Your Zoom invoice is ready",
            "reason": "manual download needed",
            "portal_link": "https://zoom.example/billing/invoices/889",
        }
    ]
    assert summary["skipped"] == [
        {
            "source_account": ENGINEERING,
            "message_id": "m-news",
            "subject": "What is new in Slack",
            "reason": "not a billing email",
        },
        {
            "source_account": ENGINEERING,
            "message_id": "m-linear",
            "subject": "Payment failed for Linear",
            "reason": "billing signal: payment failed",
        },
    ]
    assert summary["failed"] == [
        {
            "source_account": ENGINEERING,
            "message_id": "m-github",
            "subject": "Your GitHub invoice",
            "reason": "the PDF could not be read",
        }
    ]
    assert summary["billing_signals"] == [
        {
            "kind": "payment_failed",
            "vendor": "Linear",
            "source_account": ENGINEERING,
            "message_id": "m-linear",
            "subject": "Payment failed for Linear",
            "received_at": "2026-08-16T09:00:00+00:00",
        }
    ]


def test_summary_covers_only_the_chosen_month(
    dashboard: TestClient, ledger: Ledger, ledger_path: Path
) -> None:
    record_august(ledger, ledger_path)
    ledger.record(JULY, email("m-july", "July", month=7), EmailState.SKIPPED, reason="x")
    sign_in(dashboard)

    summary = dashboard.get("/api/months/2026-07/summary").json()

    assert summary["rows"] == []
    assert summary["counts"] == {"collected": 0, "needs_review": 0, "skipped": 1, "failed": 0}


def test_summary_for_a_month_with_nothing(dashboard: TestClient) -> None:
    sign_in(dashboard)

    response = dashboard.get("/api/months/2026-08/summary")

    assert response.status_code == 200
    assert response.json() == {
        "month": "2026-08",
        "rows": [],
        "totals": [],
        "total_inr": "0.00",
        "rows_without_rupees": 0,
        "counts": {"collected": 0, "needs_review": 0, "skipped": 0, "failed": 0},
        "needs_review": [],
        "skipped": [],
        "failed": [],
        "billing_signals": [],
        "gaps": [],
        "upcoming": [],
        "failed_source_accounts": [],
    }


@pytest.mark.parametrize("month", ["2026-13", "2026-8", "august", "2026-08-01", "26-08"])
def test_malformed_month_returns_422(dashboard: TestClient, month: str) -> None:
    sign_in(dashboard)

    assert dashboard.get(f"/api/months/{month}/summary").status_code == 422


def test_link_in_a_summary_row_opens_the_filed_pdf(
    dashboard: TestClient, ledger: Ledger, ledger_path: Path
) -> None:
    record_august(ledger, ledger_path)
    sign_in(dashboard)
    row = dashboard.get("/api/months/2026-08/summary").json()["rows"][0]

    response = dashboard.get(row["file_url"])

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content == b"%PDF-1.7 slack invoice"


def test_file_that_is_not_a_billing_document_of_the_month_is_not_served(
    dashboard: TestClient, ledger: Ledger, ledger_path: Path
) -> None:
    record_august(ledger, ledger_path)
    (ledger_path.parent / "archive" / "2026-08" / "stray.pdf").write_bytes(b"%PDF-1.7 stray")
    sign_in(dashboard)

    assert dashboard.get("/api/months/2026-08/billing-documents/stray.pdf").status_code == 404
    assert dashboard.get("/api/months/2026-08/billing-documents/ledger.sqlite").status_code == 404
    assert (
        dashboard.get(
            "/api/months/2026-07/billing-documents/2026-08_Slack_652.50-USD.pdf"
        ).status_code
        == 404
    )


def test_link_to_a_file_kept_elsewhere_is_passed_through(
    dashboard: TestClient, ledger: Ledger
) -> None:
    slack = Extraction("invoice", "Slack", date(2026, 8, 3), Decimal("652.50"), "USD")
    ledger.record(
        AUGUST,
        email("m-slack", "Your Slack invoice is available"),
        EmailState.COLLECTED,
        documents=(
            CollectedDocument("hash", slack, "https://drive.google.example/file/d/abc/view"),
        ),
    )
    sign_in(dashboard)

    row = dashboard.get("/api/months/2026-08/summary").json()["rows"][0]

    assert row["file_url"] == "https://drive.google.example/file/d/abc/view"


def test_summary_shows_gaps_and_source_accounts_that_could_not_be_read(
    dashboard: TestClient, ledger: Ledger
) -> None:
    ledger.save_expected_vendor(ExpectedVendor("Zoom", DESIGN, "monthly", None, None, "USD"))
    ledger.save_expected_vendor(ExpectedVendor("AWS", ENGINEERING, "monthly", None, None, "USD"))
    ledger.record_sync(CollectionMonth(2026, 8), ENGINEERING)
    ledger.record_sync(CollectionMonth(2026, 8), DESIGN, reason="sign-in expired")
    sign_in(dashboard)

    summary = dashboard.get("/api/months/2026-08/summary").json()

    assert summary["gaps"] == [
        {"vendor": "AWS", "kind": "missing", "source_account": ENGINEERING, "explanation": None},
        {
            "vendor": "Zoom",
            "kind": "unknown",
            "source_account": DESIGN,
            "explanation": f"{DESIGN} could not be read",
        },
    ]
    assert summary["failed_source_accounts"] == [
        {"source_account": DESIGN, "reason": "sign-in expired"}
    ]
