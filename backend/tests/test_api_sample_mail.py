"""Putting the sample mail into a connected source account, from the Source accounts screen.

Google is replaced by the fake connector and Gmail by a fake inserter that remembers what
each mailbox holds. The sample mail is the real sample folder.
"""

import json
from collections.abc import Callable, Iterator
from datetime import datetime
from email import message_from_bytes, policy
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import pytest
from conftest import ReplayServer
from fastapi.testclient import TestClient
from google.oauth2.credentials import Credentials
from test_api import FINANCE
from test_api import sign_in as sign_in_to_dashboard
from test_api_source_accounts import CODES, Clock, connect
from test_seed_gmail import Mailbox

from invoice_collector import google_auth
from invoice_collector.api.app import create_app
from invoice_collector.api.identity import FakeIdentityVerifier, WebClient
from invoice_collector.api.sample_mail import (
    FakeSampleMailInserter,
    GmailSampleMailInserter,
    SampleMail,
    SampleMailNotInserted,
)
from invoice_collector.api.settings import Settings
from invoice_collector.api.source_account_connector import (
    ConnectionNotCompleted,
    FakeSourceAccountConnector,
    GoogleSourceAccountConnector,
)
from invoice_collector.google_auth import GMAIL_INSERT, GMAIL_READONLY, SEEDING
from invoice_collector.ledger import Ledger

SAMPLES = Path(__file__).parents[1] / "samples"
OPS_SAMPLE = "ops@nyayalabs.example"
MAILBOX = "reviewer.test@gmail.example"
OTHER = "someone.else@gmail.example"
MEMBER = "member@nyayalabs.example"
PORTAL = "http://portal:8765"
SCREEN = "http://localhost:5173/source-accounts"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        session_secret="a-secret-only-for-tests",
        allowlist=frozenset({FINANCE}),
        ledger_path=tmp_path / "out" / "ledger.sqlite",
        token_dir=tmp_path / "tokens",
    )


@pytest.fixture
def inserter() -> FakeSampleMailInserter:
    return FakeSampleMailInserter()


@pytest.fixture
def connector() -> FakeSourceAccountConnector:
    return FakeSourceAccountConnector(
        {**CODES, "code-mailbox": MAILBOX, "code-mailbox-insert": MAILBOX, "code-other": OTHER}
    )


def client_for(
    settings: Settings,
    connector: FakeSourceAccountConnector,
    inserter: FakeSampleMailInserter,
    sample_mail: SampleMail | None,
) -> TestClient:
    app = create_app(
        settings,
        lambda: Ledger(settings.ledger_path),
        FakeIdentityVerifier({"code-finance": FINANCE, "code-member": MEMBER}),
        source_account_connector=connector,
        sample_mail=sample_mail,
        sample_mail_inserter=inserter,
        now=Clock(),
    )
    return TestClient(app, base_url="http://localhost:8000", follow_redirects=False)


@pytest.fixture
def dashboard(
    settings: Settings, connector: FakeSourceAccountConnector, inserter: FakeSampleMailInserter
) -> Iterator[TestClient]:
    with client_for(settings, connector, inserter, SampleMail(SAMPLES, PORTAL)) as client:
        sign_in_to_dashboard(client)
        response = connect(client, MAILBOX, "code-mailbox")
        assert response.headers["location"] == f"{SCREEN}?connect=connected"
        yield client


def fill_path(address: str = MAILBOX) -> str:
    return f"/api/source-accounts/{quote(address, safe='')}/sample-mail"


def ask_to_fill(
    dashboard: TestClient, mailbox: str = OPS_SAMPLE, *, vendors: bool = False
) -> dict[str, Any]:
    response = dashboard.post(
        fill_path(), json={"sample_mailbox": mailbox, "fill_expected_vendors": vendors}
    )
    assert response.status_code == 200, response.text
    return response.json()


def come_back_with_leave(dashboard: TestClient, started: dict[str, Any], code: str) -> str:
    """Google sends the person back having granted leave to insert; returns where to."""
    state = parse_qs(urlparse(str(started["authorization_url"])).query)["state"][0]
    response = dashboard.get("/accounts/callback", params={"state": state, "code": code})
    return str(response.headers["location"])


def expected_vendors(settings: Settings) -> list[tuple[str, str | None, str]]:
    ledger = Ledger(settings.ledger_path)
    try:
        return [(v.vendor, v.source_account, v.status) for v in ledger.expected_vendors()]
    finally:
        ledger.close()


def test_the_offer_lists_the_sample_mailboxes_and_their_vendors(dashboard: TestClient) -> None:
    offer = dashboard.get("/api/source-accounts/sample-mail").json()

    assert offer["available"] is True and offer["can_fill"] is True
    assert offer["portal_url"] == PORTAL
    names = [m["name"] for m in offer["mailboxes"]]
    assert names == [
        "engineering@nyayalabs.example",
        "finance@nyayalabs.example",
        "ops@nyayalabs.example",
    ]
    [ops] = [m for m in offer["mailboxes"] if m["name"] == OPS_SAMPLE]
    assert ops["emails"] == 31
    assert "Slack" in ops["vendors"] and len(ops["vendors"]) == 7


def test_leave_to_insert_is_asked_of_google_for_that_address_alone(
    dashboard: TestClient, inserter: FakeSampleMailInserter
) -> None:
    started = ask_to_fill(dashboard)

    assert started["result"] is None
    query = parse_qs(urlparse(str(started["authorization_url"])).query)
    assert query["login_hint"] == [MAILBOX]
    assert set(query["scope"][0].split()) == {GMAIL_INSERT, "openid", "email"}
    assert inserter.held == {}


def test_coming_back_stores_leave_to_insert_apart_and_inserts_the_sample_mail(
    dashboard: TestClient, inserter: FakeSampleMailInserter, settings: Settings
) -> None:
    location = come_back_with_leave(dashboard, ask_to_fill(dashboard), "code-mailbox-insert")

    assert location == f"{SCREEN}?sample-mail=filled"
    result = dashboard.get("/api/source-accounts/sample-mail-result").json()
    assert result["outcome"] == "filled"
    assert (result["address"], result["sample_mailbox"]) == (MAILBOX, OPS_SAMPLE)
    assert (result["inserted"], result["already_there"]) == (31, 0)
    assert result["vendors_added"] == []
    assert len(inserter.held[MAILBOX]) == 31
    # Stored in a file of its own; the reading sign-in keeps read-only access alone.
    seeding = google_auth.sign_in_file(MAILBOX, settings.token_dir, SEEDING)
    assert GMAIL_INSERT in json.loads(seeding.read_text(encoding="utf-8"))["scopes"]
    reading = google_auth.stored_sign_in(MAILBOX, settings.token_dir)
    assert reading is not None and reading.scopes == {GMAIL_READONLY}
    # Nothing was added to the expected vendor list, since it was not asked for.
    assert expected_vendors(settings) == []


def test_the_emails_are_addressed_to_the_mailbox_with_links_to_the_portal_the_runner_reaches(
    tmp_path: Path,
) -> None:
    messages = SampleMail(SAMPLES, PORTAL).messages_for(OPS_SAMPLE, MAILBOX)

    texts = [m.raw.decode("utf-8", "replace") for m in messages]
    assert all(message_from_bytes(m.raw, policy=policy.default)["To"] == MAILBOX for m in messages)
    assert any(f"{PORTAL}/" in text for text in texts)
    assert not any("localhost:8765" in text for text in texts)


def test_a_second_time_inserts_nothing_twice_and_needs_no_trip_to_google(
    dashboard: TestClient, inserter: FakeSampleMailInserter
) -> None:
    come_back_with_leave(dashboard, ask_to_fill(dashboard), "code-mailbox-insert")
    inserter.allowed.add(MAILBOX)

    again = ask_to_fill(dashboard)

    assert again["authorization_url"] is None
    assert (again["result"]["inserted"], again["result"]["already_there"]) == (0, 31)


def test_the_expected_vendors_are_added_only_when_chosen_and_billed_to_the_real_address(
    dashboard: TestClient, inserter: FakeSampleMailInserter, settings: Settings
) -> None:
    inserter.allowed.add(MAILBOX)
    added = dashboard.post(
        "/api/vendors", json={"vendor": "Slack", "billing_cycle": "monthly", "source_account": None}
    )
    assert added.status_code == 201

    result = ask_to_fill(dashboard, vendors=True)["result"]

    assert result["vendors_already_listed"] == ["Slack"]
    assert len(result["vendors_added"]) == 6
    listed = expected_vendors(settings)
    assert ("Notion", MAILBOX, "expected") in listed
    assert ("Slack", None, "expected") in listed
    history = dashboard.get("/api/vendors/Notion/history").json()
    assert [(c["action"], c["person"]) for c in history] == [("added", FINANCE)]


def test_filling_a_mailbox_is_recorded_among_the_changes(
    dashboard: TestClient, inserter: FakeSampleMailInserter
) -> None:
    inserter.allowed.add(MAILBOX)
    ask_to_fill(dashboard)

    [latest, *_] = dashboard.get("/api/source-accounts/history").json()
    assert (latest["address"], latest["action"], latest["person"]) == (
        MAILBOX,
        "filled_with_sample_mail",
        FINANCE,
    )


def test_leave_given_by_another_address_is_not_stored_and_nothing_is_inserted(
    dashboard: TestClient, inserter: FakeSampleMailInserter, settings: Settings
) -> None:
    location = come_back_with_leave(dashboard, ask_to_fill(dashboard), "code-other")

    assert location == f"{SCREEN}?sample-mail=failed"
    result = dashboard.get("/api/source-accounts/sample-mail-result").json()
    assert f"{OTHER} signed in at Google, not {MAILBOX}" in result["reason"]
    assert inserter.held == {}
    assert not google_auth.sign_in_file(MAILBOX, settings.token_dir, SEEDING).exists()


def test_a_failure_to_insert_is_said_in_plain_words(
    dashboard: TestClient, inserter: FakeSampleMailInserter
) -> None:
    inserter.allowed.add(MAILBOX)
    inserter.failing = "Gmail refused or could not be reached while inserting"

    response = dashboard.post(fill_path(), json={"sample_mailbox": OPS_SAMPLE})

    assert response.status_code == 502
    assert response.json()["detail"].startswith("Gmail refused")


def test_only_an_administrator_may_put_sample_mail_into_a_mailbox(
    settings: Settings,
    connector: FakeSourceAccountConnector,
    inserter: FakeSampleMailInserter,
    dashboard: TestClient,
) -> None:
    added = dashboard.post("/api/people", json={"address": MEMBER, "role": "member"})
    assert added.status_code == 201
    with client_for(settings, connector, inserter, SampleMail(SAMPLES, PORTAL)) as member:
        sign_in_to_dashboard(member, "code-member")

        offer = member.get("/api/source-accounts/sample-mail").json()
        response = member.post(fill_path(), json={"sample_mailbox": OPS_SAMPLE})

    assert offer["can_fill"] is False
    assert response.status_code == 403
    assert inserter.held == {}


def test_a_mailbox_that_is_not_connected_cannot_be_filled(dashboard: TestClient) -> None:
    response = dashboard.post(fill_path(OTHER), json={"sample_mailbox": OPS_SAMPLE})

    assert response.status_code == 404


def test_a_sample_mailbox_that_does_not_exist_is_refused(dashboard: TestClient) -> None:
    response = dashboard.post(fill_path(), json={"sample_mailbox": "nobody@nowhere.example"})

    assert response.status_code == 422
    assert "is not a sample mailbox" in response.json()["detail"]


def test_without_sample_mail_the_screen_is_told_so(
    settings: Settings, connector: FakeSourceAccountConnector, inserter: FakeSampleMailInserter
) -> None:
    with client_for(settings, connector, inserter, None) as client:
        sign_in_to_dashboard(client)

        offer = client.get("/api/source-accounts/sample-mail").json()

    assert offer["available"] is False
    assert offer["reason"] == "There is no sample mail beside this dashboard."


def test_the_sample_mail_is_found_beside_the_code_unless_another_folder_is_named(
    tmp_path: Path,
) -> None:
    environment = {"INVOICE_COLLECTOR_SESSION_SECRET": "x", "INVOICE_COLLECTOR_ALLOWLIST": FINANCE}
    ledger = tmp_path / "ledger.sqlite"

    found = Settings.from_environment(environment, ledger_path=ledger)
    named = Settings.from_environment(
        {**environment, "INVOICE_COLLECTOR_SAMPLES_DIR": str(SAMPLES)}, ledger_path=ledger
    )

    assert found.samples_dir is not None and (found.samples_dir / "golden.json").is_file()
    assert named.samples_dir == SAMPLES


# Inserting through Gmail


def store(
    address: str, token_dir: Path, token: str, scopes: list[str], purpose: str | None
) -> None:
    google_auth.store_sign_in(
        address,
        Credentials(  # pyright: ignore[reportUnknownVariableType]
            token=token,
            refresh_token=f"refresh-{token}",
            token_uri="http://127.0.0.1:9/never-called",
            client_id="made-up.apps.googleusercontent.com",
            client_secret="made-up",
            scopes=scopes,
            expiry=datetime(2999, 1, 1),
        ),
        token_dir,
        purpose,
    )


def gmail_of(mailbox: Mailbox) -> Callable[[Credentials], Any]:
    def service(credentials: Credentials) -> Any:
        token = str(credentials.token)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        return mailbox.inserter() if token == "inserting" else mailbox.reader()

    return service


def test_gmail_inserts_with_the_stored_leave_and_looks_up_with_the_reading_sign_in(
    tmp_path: Path,
) -> None:
    mailbox = Mailbox()
    store(MAILBOX, tmp_path, "reading", [GMAIL_READONLY], None)
    inserter = GmailSampleMailInserter(tmp_path, gmail_of(mailbox))
    assert not inserter.can_insert(MAILBOX)
    store(MAILBOX, tmp_path, "inserting", [GMAIL_INSERT, "openid", "email"], SEEDING)
    messages = SampleMail(SAMPLES, PORTAL).messages_for(OPS_SAMPLE, MAILBOX)

    first = inserter.insert(MAILBOX, messages)
    again = inserter.insert(MAILBOX, messages)

    assert inserter.can_insert(MAILBOX)
    assert (len(first.inserted), len(again.inserted), len(again.skipped)) == (31, 0, 31)
    assert all(message["To"] == MAILBOX for message in mailbox.inserted)


def test_gmail_without_a_reading_sign_in_inserts_nothing_and_says_so(tmp_path: Path) -> None:
    mailbox = Mailbox()
    store(MAILBOX, tmp_path, "inserting", [GMAIL_INSERT], SEEDING)
    inserter = GmailSampleMailInserter(tmp_path, gmail_of(mailbox))

    with pytest.raises(SampleMailNotInserted, match=f"The sign-in for {MAILBOX} does not work"):
        inserter.insert(MAILBOX, SampleMail(SAMPLES).messages_for(OPS_SAMPLE, MAILBOX))

    assert mailbox.inserted == []


# Google's answer to asking for leave to insert


TOKEN_ANSWER = {
    "access_token": "made-up-access",
    "refresh_token": "made-up-refresh",
    "expires_in": 3599,
    "scope": f"{GMAIL_INSERT} openid https://www.googleapis.com/auth/userinfo.email",
    "id_token": "made-up-id-token",
}


def google_connector(token_endpoint: str, claims: dict[str, Any]) -> GoogleSourceAccountConnector:
    seen: list[tuple[str, str]] = []

    def verify(token: str, audience: str) -> dict[str, Any]:
        seen.append((token, audience))
        assert (token, audience) == ("made-up-id-token", "made-up-id")
        return claims

    return GoogleSourceAccountConnector(
        WebClient("made-up-id", "made-up-value"),
        "http://localhost:8000/accounts/callback",
        token_endpoint=token_endpoint,
        verify_id_token=verify,
    )


def test_the_address_that_gave_leave_to_insert_is_the_one_google_vouches_for(
    replay_server: ReplayServer,
) -> None:
    connector = google_connector(
        replay_server(200, TOKEN_ANSWER), {"email": MAILBOX, "email_verified": True}
    )

    signed_in = connector.signed_in_to_insert("a-code")

    assert signed_in.address == MAILBOX
    assert GMAIL_INSERT in (signed_in.credentials.scopes or [])  # pyright: ignore[reportUnknownMemberType]


def test_an_address_google_has_not_verified_gives_no_leave(replay_server: ReplayServer) -> None:
    connector = google_connector(
        replay_server(200, TOKEN_ANSWER), {"email": MAILBOX, "email_verified": False}
    )

    with pytest.raises(ConnectionNotCompleted, match="not verified"):
        connector.signed_in_to_insert("a-code")
