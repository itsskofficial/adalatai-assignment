"""The Source accounts screen's API, called the way the dashboard calls it.

Google is replaced by a fake connector, and by a local server replaying recorded answers
where a stored sign-in is refreshed. Sign-ins are stored in a temporary token folder.
"""

import json
import logging
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, quote, urlparse

import httpx2
import pytest
from conftest import ReplayClient, ReplayServer
from fastapi.testclient import TestClient
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build  # pyright: ignore[reportUnknownVariableType]
from googleapiclient.http import HttpMockSequence
from test_api import AUGUST, DESIGN, ENGINEERING, FINANCE, FRONT_END, JULY, collected, email
from test_api import sign_in as sign_in_to_dashboard
from test_cli import Mailboxes, collect_from_gmail

from invoice_collector import google_auth
from invoice_collector.api.app import create_app
from invoice_collector.api.identity import FakeIdentityVerifier, WebClient
from invoice_collector.api.settings import Settings
from invoice_collector.api.source_account_connector import (
    FAKE_CLIENT_SECRET,
    FakeSourceAccountConnector,
    GoogleSourceAccountConnector,
    fake_access_token,
    fake_refresh_token,
)
from invoice_collector.domain import EmailState, Extraction
from invoice_collector.google_auth import DRIVE_FILE, GMAIL_INSERT, GMAIL_READONLY
from invoice_collector.ledger import Ledger
from invoice_collector.source_account_registry import connected_source_accounts

NOW = datetime(2026, 9, 29, 10, 30, tzinfo=UTC)
OPS = "ops@nyayalabs.example"
FOUND = "found@nyayalabs.example"
MEMBER = "member@nyayalabs.example"
RECORDED = Path(__file__).parent / "recorded"
REVOKED = json.loads((RECORDED / "google_sign_in_revoked.json").read_text("utf-8"))
CALLBACK = "http://localhost:8000/accounts/callback"
SCREEN = f"{FRONT_END}/source-accounts"

# Each code is a sign-in by the address it names.
CODES = {
    "code-engineering": ENGINEERING,
    "code-engineering-again": ENGINEERING,
    "code-design": DESIGN,
    "code-design-again": DESIGN,
    "code-finance-mailbox": FINANCE,
}


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def token_dir(tmp_path: Path) -> Path:
    return tmp_path / "tokens"


@pytest.fixture
def ledger_path(tmp_path: Path) -> Path:
    # Where collect --out tmp_path/out keeps its ledger.
    return tmp_path / "out" / "ledger.sqlite"


@pytest.fixture
def ledger(ledger_path: Path) -> Iterator[Ledger]:
    ledger = Ledger(ledger_path)
    yield ledger
    ledger.close()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def connector() -> FakeSourceAccountConnector:
    return FakeSourceAccountConnector(dict(CODES))


@pytest.fixture
def settings(ledger_path: Path, token_dir: Path) -> Settings:
    return Settings(
        session_secret="a-secret-only-for-tests",
        allowlist=frozenset({FINANCE, OPS}),
        ledger_path=ledger_path,
        token_dir=token_dir,
    )


def client_for(settings: Settings, connector: Any, clock: Clock | None = None) -> TestClient:
    verifier = FakeIdentityVerifier(
        {"code-finance": FINANCE, "code-ops": OPS, "code-member": MEMBER}
    )
    app = create_app(
        settings,
        lambda: Ledger(settings.ledger_path),
        verifier,
        source_account_connector=connector,
        now=clock or Clock(),
    )
    return TestClient(app, base_url="http://localhost:8000", follow_redirects=False)


@pytest.fixture
def app_client(
    settings: Settings, connector: FakeSourceAccountConnector, clock: Clock
) -> Iterator[TestClient]:
    with client_for(settings, connector, clock) as client:
        yield client


@pytest.fixture
def dashboard(app_client: TestClient) -> TestClient:
    sign_in_to_dashboard(app_client)
    return app_client


def path_of(address: str) -> str:
    return f"/api/source-accounts/{quote(address, safe='')}"


def google_query(response: httpx2.Response) -> dict[str, list[str]]:
    assert response.status_code == 200, response.text
    return parse_qs(urlparse(str(response.json()["authorization_url"])).query)


def begin_connecting(dashboard: TestClient, address: str, *, owner: bool = False) -> str:
    """Presses Connect and returns the state sent to Google."""
    response = dashboard.post(
        "/api/source-accounts/connect", json={"address": address, "owner": owner}
    )
    return google_query(response)["state"][0]


def come_back(dashboard: TestClient, state: str, code: str) -> httpx2.Response:
    """Google sends the person back to the dashboard."""
    return dashboard.get("/accounts/callback", params={"state": state, "code": code})


def connect(
    dashboard: TestClient, address: str, code: str, *, owner: bool = False
) -> httpx2.Response:
    return come_back(dashboard, begin_connecting(dashboard, address, owner=owner), code)


def listed(dashboard: TestClient) -> dict[str, Any]:
    response = dashboard.get("/api/source-accounts")
    assert response.status_code == 200
    return response.json()


def account_in(dashboard: TestClient, address: str) -> dict[str, Any]:
    [account] = [a for a in listed(dashboard)["source_accounts"] if a["address"] == address]
    return account


def no_browser(client_file: Path, scopes: Any) -> Credentials:
    raise AssertionError("the browser must not be opened")


def no_gmail(credentials: Credentials) -> Any:
    raise AssertionError("Gmail must not be called")


def load_as_the_command_line_does(address: str, token_dir: Path) -> Credentials:
    return google_auth.sign_in(
        address,
        [GMAIL_READONLY],
        token_dir,
        allow_browser=False,
        browser_flow=no_browser,
        gmail_service=no_gmail,
    )


def store_expired_sign_in(address: str, token_dir: Path, token_uri: str) -> None:
    """A sign-in whose access has run out and whose refresh Google will refuse."""
    google_auth.store_sign_in(
        address,
        Credentials(  # pyright: ignore[reportUnknownVariableType]
            token="old-access-value",
            refresh_token="old-refresh-value",
            token_uri=token_uri,
            client_id="made-up-client.apps.googleusercontent.com",
            client_secret="made-up",
            scopes=[GMAIL_READONLY],
            expiry=datetime(2020, 1, 1),
        ),
        token_dir,
    )


def store_command_line_sign_in(
    address: str, token_dir: Path, scopes: list[str], purpose: str | None = None
) -> None:
    """A sign-in stored by invoice-collector-setup, valid far into the future."""
    credentials = Credentials(  # pyright: ignore[reportUnknownVariableType]
        token="cli-access-value",
        refresh_token="cli-refresh-value",
        token_uri="http://127.0.0.1:9/never-called",
        client_id="made-up-client.apps.googleusercontent.com",
        client_secret="made-up",
        scopes=scopes,
        expiry=datetime(2999, 1, 1),
    )
    path = google_auth.sign_in_file(address, token_dir, purpose)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        credentials.to_json(),  # pyright: ignore[reportUnknownMemberType]
        encoding="utf-8",
    )


# Connecting


def test_connecting_sends_the_person_to_google_for_read_only_gmail_access(
    tmp_path: Path, settings: Settings
) -> None:
    client_file = tmp_path / "web-client.json"
    client_file.write_text(
        '{"web": {"client_id": "made-up-id", "client_secret": "made-up-value"}}',
        encoding="utf-8",
    )
    connector = GoogleSourceAccountConnector(WebClient.read(client_file), CALLBACK)
    with client_for(settings, connector) as dashboard:
        sign_in_to_dashboard(dashboard)

        response = dashboard.post(
            "/api/source-accounts/connect", json={"address": ENGINEERING, "owner": False}
        )

    location = urlparse(str(response.json()["authorization_url"]))
    query = parse_qs(location.query)
    assert location.netloc == "accounts.google.com"
    assert len(query.pop("state")[0]) >= 32
    assert query == {
        "client_id": ["made-up-id"],
        "redirect_uri": [CALLBACK],
        "response_type": ["code"],
        "scope": [GMAIL_READONLY],
        "access_type": ["offline"],
        "prompt": ["consent"],
        "login_hint": [ENGINEERING],
    }
    assert "made-up-value" not in response.text


def test_the_owner_account_is_also_asked_for_the_drive_files_the_tool_creates(
    dashboard: TestClient,
) -> None:
    response = dashboard.post(
        "/api/source-accounts/connect", json={"address": ENGINEERING, "owner": True}
    )

    assert google_query(response)["scope"] == [f"{GMAIL_READONLY} {DRIVE_FILE}"]
    assert google_query(response)["login_hint"] == [ENGINEERING]


def test_an_address_that_is_not_an_email_address_is_refused(dashboard: TestClient) -> None:
    response = dashboard.post(
        "/api/source-accounts/connect", json={"address": "not an address", "owner": False}
    )

    assert response.status_code == 422


def test_connected_source_account_is_stored_where_the_command_line_finds_it(
    dashboard: TestClient, token_dir: Path
) -> None:
    response = connect(dashboard, ENGINEERING, "code-engineering")

    assert response.status_code in (302, 307)
    assert response.headers["location"] == f"{SCREEN}?connect=connected"
    credentials = load_as_the_command_line_does(ENGINEERING, token_dir)
    assert credentials.refresh_token == fake_refresh_token("code-engineering")  # pyright: ignore[reportUnknownMemberType]
    assert dashboard.get("/api/source-accounts/connection-result").json() == {
        "outcome": "connected",
        "address": ENGINEERING,
        "signed_in_address": ENGINEERING,
        "reason": None,
    }
    assert [a["address"] for a in listed(dashboard)["source_accounts"]] == [ENGINEERING]


def test_address_is_matched_whatever_its_letter_case(
    dashboard: TestClient, token_dir: Path
) -> None:
    connect(dashboard, "Engineering@NyayaLabs.example", "code-engineering")

    assert [a["address"] for a in listed(dashboard)["source_accounts"]] == [ENGINEERING]
    load_as_the_command_line_does(ENGINEERING, token_dir)


def test_callback_with_a_wrong_state_is_refused(
    dashboard: TestClient, connector: FakeSourceAccountConnector, token_dir: Path
) -> None:
    begin_connecting(dashboard, ENGINEERING)

    response = come_back(dashboard, "not-the-state", "code-engineering")

    assert response.headers["location"] == f"{SCREEN}?connect=failed"
    assert connector.codes_exchanged == []
    assert not token_dir.exists()
    assert listed(dashboard)["source_accounts"] == []


def test_a_state_cannot_be_used_twice(dashboard: TestClient) -> None:
    state = begin_connecting(dashboard, ENGINEERING)
    come_back(dashboard, state, "code-engineering")

    response = come_back(dashboard, state, "code-engineering-again")

    assert response.headers["location"] == f"{SCREEN}?connect=failed"


def test_the_wrong_address_signing_in_is_refused_and_nothing_is_stored(
    dashboard: TestClient, token_dir: Path, ledger_path: Path
) -> None:
    response = connect(dashboard, ENGINEERING, "code-design")

    assert response.headers["location"] == f"{SCREEN}?connect=wrong-address"
    assert dashboard.get("/api/source-accounts/connection-result").json() == {
        "outcome": "wrong_address",
        "address": ENGINEERING,
        "signed_in_address": DESIGN,
        "reason": None,
    }
    assert not google_auth.sign_in_file(ENGINEERING, token_dir).exists()
    assert not google_auth.sign_in_file(DESIGN, token_dir).exists()
    assert listed(dashboard)["source_accounts"] == []
    assert connected_source_accounts(ledger_path) == []


def test_a_callback_in_another_persons_session_is_refused(
    settings: Settings, connector: FakeSourceAccountConnector, token_dir: Path
) -> None:
    with (
        client_for(settings, connector) as finance,
        client_for(settings, connector) as ops,
    ):
        sign_in_to_dashboard(finance, "code-finance")
        sign_in_to_dashboard(ops, "code-ops")
        state = begin_connecting(finance, ENGINEERING)

        response = come_back(ops, state, "code-engineering")

        assert response.headers["location"] == f"{SCREEN}?connect=failed"
        assert connector.codes_exchanged == []
        assert not token_dir.exists()


def test_a_callback_without_a_signed_in_person_is_refused(
    app_client: TestClient, connector: FakeSourceAccountConnector
) -> None:
    response = come_back(app_client, "any-state", "code-engineering")

    assert response.headers["location"].startswith(SCREEN)
    assert connector.codes_exchanged == []


def test_a_member_on_the_people_list_can_connect_a_source_account(
    settings: Settings, connector: FakeSourceAccountConnector
) -> None:
    with (
        client_for(settings, connector) as administrator,
        client_for(settings, connector) as member,
    ):
        sign_in_to_dashboard(administrator, "code-finance")
        added = administrator.post("/api/people", json={"address": MEMBER, "role": "member"})
        assert added.status_code == 201
        sign_in_to_dashboard(member, "code-member")

        response = connect(member, ENGINEERING, "code-engineering")

        assert response.headers["location"] == f"{SCREEN}?connect=connected"
        assert account_in(member, ENGINEERING)["connected_by"] == MEMBER


def test_a_person_removed_while_at_google_cannot_finish_connecting(
    settings: Settings, connector: FakeSourceAccountConnector, token_dir: Path
) -> None:
    with (
        client_for(settings, connector) as administrator,
        client_for(settings, connector) as member,
    ):
        sign_in_to_dashboard(administrator, "code-finance")
        added = administrator.post("/api/people", json={"address": MEMBER, "role": "member"})
        assert added.status_code == 201
        sign_in_to_dashboard(member, "code-member")
        state = begin_connecting(member, ENGINEERING)
        removed = administrator.delete(f"/api/people/{quote(MEMBER, safe='')}")
        assert removed.status_code == 204

        response = come_back(member, state, "code-engineering")

        assert response.headers["location"] == f"{SCREEN}?connect=failed"
        assert connector.codes_exchanged == []
        assert not token_dir.exists()


def test_google_refusing_the_code_is_shown_and_nothing_is_stored(
    dashboard: TestClient, token_dir: Path
) -> None:
    response = connect(dashboard, ENGINEERING, "code-unknown")

    assert response.headers["location"] == f"{SCREEN}?connect=failed"
    result = dashboard.get("/api/source-accounts/connection-result").json()
    assert result["outcome"] == "failed"
    assert result["reason"] == "Google refused the authorization code"
    assert not token_dir.exists()


def test_access_not_granted_at_google_is_shown(dashboard: TestClient) -> None:
    state = begin_connecting(dashboard, ENGINEERING)

    response = dashboard.get(
        "/accounts/callback", params={"state": state, "error": "access_denied"}
    )

    assert response.headers["location"] == f"{SCREEN}?connect=failed"
    result = dashboard.get("/api/source-accounts/connection-result").json()
    assert result["reason"] == "Access was not granted at Google"


def test_connecting_an_account_that_is_already_connected_is_refused(
    dashboard: TestClient,
) -> None:
    connect(dashboard, ENGINEERING, "code-engineering")

    response = dashboard.post(
        "/api/source-accounts/connect", json={"address": ENGINEERING, "owner": False}
    )

    assert response.status_code == 409
    assert "Renew" in response.json()["detail"]


# The state of each sign-in


def test_each_state_of_a_sign_in_is_listed(
    dashboard: TestClient, token_dir: Path, replay_server: ReplayServer
) -> None:
    connect(dashboard, ENGINEERING, "code-engineering")
    connect(dashboard, DESIGN, "code-design")
    connect(dashboard, FINANCE, "code-finance-mailbox")
    store_expired_sign_in(DESIGN, token_dir, replay_server(400, REVOKED))
    # A sign-in deleted by hand from the token folder.
    google_auth.forget_sign_in(ENGINEERING, token_dir)

    states = {a["address"]: a["sign_in"] for a in listed(dashboard)["source_accounts"]}

    assert states == {ENGINEERING: "missing", DESIGN: "expired", FINANCE: "works"}


def test_a_working_sign_in_says_when_it_will_stop_working(
    dashboard: TestClient, clock: Clock
) -> None:
    connect(dashboard, ENGINEERING, "code-engineering")
    clock.now = NOW + timedelta(days=1)

    account = account_in(dashboard, ENGINEERING)

    assert account["sign_in"] == "works"
    assert account["sign_in_ends_at"] == (NOW + timedelta(days=7)).isoformat()
    assert account["expiring_soon"] is False


def test_a_sign_in_that_stops_working_within_two_days_is_expiring_soon(
    dashboard: TestClient, clock: Clock
) -> None:
    connect(dashboard, ENGINEERING, "code-engineering")
    clock.now = NOW + timedelta(days=5, hours=1)

    account = account_in(dashboard, ENGINEERING)

    assert account["sign_in"] == "works"
    assert account["expiring_soon"] is True


def test_a_sign_in_older_than_seven_days_is_expired_without_asking_google(
    dashboard: TestClient, clock: Clock
) -> None:
    connect(dashboard, ENGINEERING, "code-engineering")
    clock.now = NOW + timedelta(days=7, minutes=1)

    account = account_in(dashboard, ENGINEERING)

    assert account["sign_in"] == "expired"
    assert account["expiring_soon"] is False


def test_sign_ins_have_no_end_for_a_published_app(
    settings: Settings, connector: FakeSourceAccountConnector, clock: Clock
) -> None:
    published = replace(settings, sign_in_lifetime_days=None)
    with client_for(published, connector, clock) as dashboard:
        sign_in_to_dashboard(dashboard)
        connect(dashboard, ENGINEERING, "code-engineering")
        clock.now = NOW + timedelta(days=30)

        account = account_in(dashboard, ENGINEERING)

    assert account["sign_in"] == "works"
    assert account["sign_in_ends_at"] is None
    assert account["expiring_soon"] is False


def test_a_sign_in_replaced_from_the_command_line_is_not_given_the_old_end(
    dashboard: TestClient, clock: Clock, token_dir: Path
) -> None:
    connect(dashboard, ENGINEERING, "code-engineering")
    store_command_line_sign_in(ENGINEERING, token_dir, [GMAIL_READONLY])
    clock.now = NOW + timedelta(days=8)

    account = account_in(dashboard, ENGINEERING)

    assert account["sign_in"] == "works"
    assert account["sign_in_ends_at"] is None


def test_each_account_shows_its_last_read_and_what_the_latest_run_found(
    dashboard: TestClient, ledger: Ledger, ledger_path: Path
) -> None:
    connect(dashboard, ENGINEERING, "code-engineering")
    connect(dashboard, DESIGN, "code-design")
    for message_id, vendor, total in (("m-notion", "Notion", "221.40"), ("m-figma", "Figma", "9")):
        extraction = Extraction("invoice", vendor, date(2026, 8, 9), Decimal(total), "USD")
        ledger.record(
            AUGUST,
            email(message_id, f"{vendor} invoice", account=DESIGN, day=9),
            EmailState.COLLECTED,
            documents=(collected(ledger_path, extraction, f"%PDF {vendor}".encode()),),
        )
    ledger.record_sync(JULY, ENGINEERING)
    ledger.record_sync(JULY, DESIGN)
    ledger.record_sync(AUGUST, DESIGN)
    reason = f"the sign-in for {ENGINEERING} no longer works (it expired or was revoked)"
    ledger.record_sync(AUGUST, ENGINEERING, reason)

    engineering = account_in(dashboard, ENGINEERING)
    design = account_in(dashboard, DESIGN)

    assert engineering["last_read_month"] == "2026-07"
    assert engineering["latest_run"] == {
        "month": "2026-08",
        "read": False,
        "reason": reason,
        "billing_documents": 0,
    }
    assert design["last_read_month"] == "2026-08"
    assert design["latest_run"] == {
        "month": "2026-08",
        "read": True,
        "reason": None,
        "billing_documents": 2,
    }


def test_an_account_never_read_has_no_last_read(dashboard: TestClient) -> None:
    connect(dashboard, ENGINEERING, "code-engineering")

    account = account_in(dashboard, ENGINEERING)

    assert account["last_read_month"] is None
    assert account["latest_run"] is None
    assert account["connected_by"] == FINANCE
    assert account["connected_at"] == NOW.isoformat()


# Renewing, removing and the owner account


def test_renewing_sends_the_person_to_google_for_the_same_account(
    dashboard: TestClient, clock: Clock, token_dir: Path
) -> None:
    connect(dashboard, ENGINEERING, "code-engineering")
    clock.now = NOW + timedelta(days=6)

    query = google_query(dashboard.post(f"{path_of(ENGINEERING)}/renew"))
    response = come_back(dashboard, query["state"][0], "code-engineering-again")

    assert query["login_hint"] == [ENGINEERING]
    assert query["scope"] == [GMAIL_READONLY]
    assert response.headers["location"] == f"{SCREEN}?connect=renewed"
    credentials = load_as_the_command_line_does(ENGINEERING, token_dir)
    assert credentials.refresh_token == fake_refresh_token("code-engineering-again")  # pyright: ignore[reportUnknownMemberType]
    account = account_in(dashboard, ENGINEERING)
    assert account["sign_in_ends_at"] == (NOW + timedelta(days=13)).isoformat()
    assert account["connected_at"] == NOW.isoformat()


def test_only_a_connected_account_can_be_renewed(dashboard: TestClient) -> None:
    assert dashboard.post(f"{path_of(ENGINEERING)}/renew").status_code == 404


def test_renewing_the_wrong_address_keeps_the_sign_in_there_was(
    dashboard: TestClient, token_dir: Path
) -> None:
    connect(dashboard, ENGINEERING, "code-engineering")

    query = google_query(dashboard.post(f"{path_of(ENGINEERING)}/renew"))
    response = come_back(dashboard, query["state"][0], "code-design")

    assert response.headers["location"] == f"{SCREEN}?connect=wrong-address"
    credentials = load_as_the_command_line_does(ENGINEERING, token_dir)
    assert credentials.refresh_token == fake_refresh_token("code-engineering")  # pyright: ignore[reportUnknownMemberType]


def test_removing_deletes_the_stored_sign_in_and_keeps_what_was_collected(
    dashboard: TestClient, ledger: Ledger, ledger_path: Path, token_dir: Path
) -> None:
    connect(dashboard, ENGINEERING, "code-engineering")
    extraction = Extraction("invoice", "Slack", date(2026, 8, 3), Decimal("652.50"), "USD")
    ledger.record(
        AUGUST,
        email("m-slack", "Your Slack invoice"),
        EmailState.COLLECTED,
        documents=(collected(ledger_path, extraction, b"%PDF slack"),),
    )
    ledger.record_sync(AUGUST, ENGINEERING)

    response = dashboard.delete(path_of(ENGINEERING))

    assert response.status_code == 204
    assert not google_auth.sign_in_file(ENGINEERING, token_dir).exists()
    assert listed(dashboard)["source_accounts"] == []
    assert listed(dashboard)["found_on_this_machine"] == []
    assert [d.source_account for d in ledger.documents(AUGUST)] == [ENGINEERING]
    assert connected_source_accounts(ledger_path) == []


def test_removing_an_account_that_is_not_connected_is_refused(dashboard: TestClient) -> None:
    assert dashboard.delete(path_of(ENGINEERING)).status_code == 404


def test_the_owner_account_cannot_be_removed(dashboard: TestClient, token_dir: Path) -> None:
    connect(dashboard, ENGINEERING, "code-engineering", owner=True)

    response = dashboard.delete(path_of(ENGINEERING))

    assert response.status_code == 409
    assert "owner account" in response.json()["detail"]
    assert google_auth.sign_in_file(ENGINEERING, token_dir).exists()
    assert account_in(dashboard, ENGINEERING)["is_owner"] is True


def test_connecting_the_owner_account_grants_drive_access(dashboard: TestClient) -> None:
    connect(dashboard, ENGINEERING, "code-engineering", owner=True)

    account = account_in(dashboard, ENGINEERING)

    assert account["is_owner"] is True
    assert account["needs_drive_access"] is False


def test_making_another_account_the_owner_moves_ownership_and_asks_for_renewal(
    dashboard: TestClient,
) -> None:
    connect(dashboard, ENGINEERING, "code-engineering", owner=True)
    connect(dashboard, DESIGN, "code-design")

    response = dashboard.post(f"{path_of(DESIGN)}/make-owner")

    assert response.status_code == 200
    answer = response.json()
    assert answer["address"] == DESIGN
    assert answer["needs_renewal"] is True
    assert "Renew" in answer["message"]
    owners = {a["address"]: a["is_owner"] for a in listed(dashboard)["source_accounts"]}
    assert owners == {DESIGN: True, ENGINEERING: False}
    assert account_in(dashboard, DESIGN)["needs_drive_access"] is True
    query = google_query(dashboard.post(f"{path_of(DESIGN)}/renew"))
    assert query["scope"] == [f"{GMAIL_READONLY} {DRIVE_FILE}"]
    come_back(dashboard, query["state"][0], "code-design-again")
    assert account_in(dashboard, DESIGN)["needs_drive_access"] is False


def test_the_former_owner_can_be_removed_once_ownership_moves(dashboard: TestClient) -> None:
    connect(dashboard, ENGINEERING, "code-engineering", owner=True)
    connect(dashboard, DESIGN, "code-design")
    dashboard.post(f"{path_of(DESIGN)}/make-owner")

    assert dashboard.delete(path_of(ENGINEERING)).status_code == 204


def test_connecting_a_new_owner_account_takes_ownership(dashboard: TestClient) -> None:
    connect(dashboard, ENGINEERING, "code-engineering", owner=True)

    connect(dashboard, DESIGN, "code-design", owner=True)

    owners = [a["address"] for a in listed(dashboard)["source_accounts"] if a["is_owner"]]
    assert owners == [DESIGN]


def test_only_a_connected_account_can_be_made_the_owner(dashboard: TestClient) -> None:
    assert dashboard.post(f"{path_of(DESIGN)}/make-owner").status_code == 404


# Accounts found on this machine


def test_accounts_signed_in_from_the_command_line_are_found_on_this_machine(
    dashboard: TestClient, token_dir: Path
) -> None:
    connect(dashboard, ENGINEERING, "code-engineering")
    store_command_line_sign_in(FOUND, token_dir, [GMAIL_READONLY])
    # A sign-in only for putting sample emails into a mailbox cannot read it.
    store_command_line_sign_in(OPS, token_dir, [GMAIL_INSERT], purpose="seeding")

    assert listed(dashboard)["found_on_this_machine"] == [FOUND]


def test_an_account_found_on_this_machine_can_be_added(
    dashboard: TestClient, token_dir: Path, ledger_path: Path
) -> None:
    store_command_line_sign_in(FOUND, token_dir, [GMAIL_READONLY])

    response = dashboard.post(f"{path_of(FOUND)}/add")

    assert response.status_code == 201
    listing = listed(dashboard)
    assert listing["found_on_this_machine"] == []
    [account] = listing["source_accounts"]
    assert account["address"] == FOUND
    assert account["sign_in"] == "works"
    assert account["sign_in_ends_at"] is None
    assert connected_source_accounts(ledger_path) == [FOUND]


def test_only_an_account_with_a_stored_sign_in_can_be_added(dashboard: TestClient) -> None:
    assert dashboard.post(f"{path_of(FOUND)}/add").status_code == 404


# History


def test_history_records_who_changed_the_source_accounts_and_when(
    settings: Settings, connector: FakeSourceAccountConnector, clock: Clock, token_dir: Path
) -> None:
    store_command_line_sign_in(FOUND, token_dir, [GMAIL_READONLY])
    with client_for(settings, connector, clock) as dashboard:
        sign_in_to_dashboard(dashboard, "code-finance")
        connect(dashboard, ENGINEERING, "code-engineering", owner=True)
        clock.now = NOW + timedelta(minutes=1)
        connect(dashboard, DESIGN, "code-design")
        sign_in_to_dashboard(dashboard, "code-ops")
        clock.now = NOW + timedelta(minutes=2)
        query = google_query(dashboard.post(f"{path_of(DESIGN)}/renew"))
        come_back(dashboard, query["state"][0], "code-design-again")
        clock.now = NOW + timedelta(minutes=3)
        dashboard.post(f"{path_of(DESIGN)}/make-owner")
        clock.now = NOW + timedelta(minutes=4)
        dashboard.delete(path_of(ENGINEERING))
        clock.now = NOW + timedelta(minutes=5)
        dashboard.post(f"{path_of(FOUND)}/add")

        history = dashboard.get("/api/source-accounts/history").json()

    at = [(NOW + timedelta(minutes=m)).isoformat() for m in range(6)]
    assert history == [
        {"address": FOUND, "action": "added", "person": OPS, "changed_at": at[5]},
        {"address": ENGINEERING, "action": "removed", "person": OPS, "changed_at": at[4]},
        {"address": DESIGN, "action": "made_owner", "person": OPS, "changed_at": at[3]},
        {"address": DESIGN, "action": "renewed", "person": OPS, "changed_at": at[2]},
        {"address": DESIGN, "action": "connected", "person": FINANCE, "changed_at": at[1]},
        {"address": ENGINEERING, "action": "made_owner", "person": FINANCE, "changed_at": at[0]},
        {"address": ENGINEERING, "action": "connected", "person": FINANCE, "changed_at": at[0]},
    ]


# Secrets and signing in


def test_no_response_holds_a_token_or_a_secret(
    dashboard: TestClient, token_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    responses: list[httpx2.Response] = []
    store_command_line_sign_in(FOUND, token_dir, [GMAIL_READONLY])
    state = begin_connecting(dashboard, ENGINEERING, owner=True)
    responses.append(come_back(dashboard, state, "code-engineering"))
    responses.append(connect(dashboard, DESIGN, "code-design"))
    responses.append(dashboard.get("/api/source-accounts/connection-result"))
    responses.append(dashboard.get("/api/source-accounts"))
    responses.append(dashboard.post(f"{path_of(DESIGN)}/renew"))
    responses.append(dashboard.post(f"{path_of(DESIGN)}/make-owner"))
    responses.append(dashboard.post(f"{path_of(FOUND)}/add"))
    responses.append(dashboard.get("/api/source-accounts/history"))
    responses.append(dashboard.delete(path_of(ENGINEERING)))

    secrets = [
        fake_access_token("code-engineering"),
        fake_refresh_token("code-engineering"),
        fake_access_token("code-design"),
        fake_refresh_token("code-design"),
        FAKE_CLIENT_SECRET,
        "cli-access-value",
        "cli-refresh-value",
    ]
    seen = "\n".join(
        f"{response.headers}\n{response.text}\n{response.cookies}" for response in responses
    )
    seen += "\n" + str(dict(dashboard.cookies)) + "\n" + caplog.text
    for secret in secrets:
        assert secret not in seen


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/source-accounts"),
        ("GET", "/api/source-accounts/history"),
        ("GET", "/api/source-accounts/connection-result"),
        ("POST", "/api/source-accounts/connect"),
        ("POST", f"/api/source-accounts/{ENGINEERING}/renew"),
        ("POST", f"/api/source-accounts/{ENGINEERING}/make-owner"),
        ("POST", f"/api/source-accounts/{ENGINEERING}/add"),
        ("DELETE", f"/api/source-accounts/{ENGINEERING}"),
    ],
)
def test_source_account_routes_return_401_when_not_signed_in(
    app_client: TestClient, method: str, path: str
) -> None:
    response = app_client.request(method, path, json={"address": ENGINEERING, "owner": False})

    assert response.status_code == 401


# Google's side, and the run


def test_the_code_is_exchanged_with_google_and_the_address_looked_up_in_gmail(
    replay_server: ReplayServer, received_requests: list[dict[str, Any]], tmp_path: Path
) -> None:
    token_endpoint = replay_server(
        200,
        {
            "access_token": "recorded-access-value",
            "refresh_token": "recorded-refresh-value",
            "expires_in": 3599,
            "scope": GMAIL_READONLY,
            "token_type": "Bearer",
        },
    )
    profile = (RECORDED / "gmail_profile.json").read_text("utf-8")

    def gmail(credentials: Credentials) -> Any:
        return cast(
            Any, build("gmail", "v1", http=HttpMockSequence([({"status": "200"}, profile)]))
        )

    connector = GoogleSourceAccountConnector(
        WebClient("made-up-id", "made-up-value"),
        CALLBACK,
        token_endpoint=token_endpoint,
        gmail_service=gmail,
    )

    signed_in = connector.signed_in("the-code", [GMAIL_READONLY])

    assert signed_in.address == "finance@acme.test"
    assert signed_in.credentials.refresh_token == "recorded-refresh-value"  # pyright: ignore[reportUnknownMemberType]
    assert list(signed_in.credentials.scopes or []) == [GMAIL_READONLY]  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    [sent] = received_requests
    assert parse_qs(sent["form"]) == {
        "code": ["the-code"],
        "client_id": ["made-up-id"],
        "client_secret": ["made-up-value"],
        "redirect_uri": [CALLBACK],
        "grant_type": ["authorization_code"],
    }


def test_collect_reads_every_connected_source_account(
    dashboard: TestClient, tmp_path: Path, replay_client: ReplayClient
) -> None:
    connect(dashboard, ENGINEERING, "code-engineering")
    connect(dashboard, DESIGN, "code-design")
    mailboxes = Mailboxes()

    exit_code = collect_from_gmail(tmp_path, replay_client, mailboxes, "--connected-accounts")

    assert exit_code == 0
    assert mailboxes.asked == [(DESIGN, tmp_path / "tokens"), (ENGINEERING, tmp_path / "tokens")]


def test_collect_with_no_connected_source_account_says_how_to_connect_one(
    tmp_path: Path, replay_client: ReplayClient, capsys: pytest.CaptureFixture[str]
) -> None:
    mailboxes = Mailboxes()

    exit_code = collect_from_gmail(tmp_path, replay_client, mailboxes, "--connected-accounts")

    assert exit_code == 2
    assert "No source account is connected" in capsys.readouterr().err
    assert mailboxes.asked == []
