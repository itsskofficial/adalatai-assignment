"""The owner account: chosen on the Source accounts screen, and never a reason not to start.

A reviewer who sets INVOICE_COLLECTOR_GOOGLE_OWNER before connecting that account must still
reach the Source accounts screen that connects it. So the dashboard and the runner start
whatever the owner account's sign-in, say so, and look the owner account up again each time
it is needed: the one chosen on the screen, or else the one the setting names.
"""

from datetime import UTC, datetime
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from fake_google import FakeDrive, FakeSheets
from fastapi.testclient import TestClient
from google.oauth2.credentials import Credentials
from support import Collection
from test_api import FINANCE, sign_in
from test_api_assisted_download import ZOOM, ZOOM_PDF, CountingExtractor, flag_zoom, upload
from test_api_review import approve, hold_slack, summary_rows
from test_api_runs import RUNS, Clock, runs_when_done
from test_api_runs import open_dashboard as open_runs_dashboard
from test_cli import OWNER, Mailboxes, store_owner_sign_in
from test_collection_runner import no_browser
from test_run_checks import SLACK_PDF

from invoice_collector.api.app import create_app
from invoice_collector.api.collection_runner import CollectionRunner, parse_run_options
from invoice_collector.api.identity import FakeIdentityVerifier
from invoice_collector.api.owner_drive import GoogleOwnerDrive
from invoice_collector.api.settings import Settings
from invoice_collector.cli import OwnerNotSignedIn, run_collection
from invoice_collector.digest import FakeDigestSender
from invoice_collector.domain import CollectionMonth
from invoice_collector.drive_archive import SCOPES as DRIVE_SCOPES
from invoice_collector.exchange_rates import FakeExchangeRates
from invoice_collector.ledger import Ledger
from invoice_collector.runner import serve as runner_serve
from invoice_collector.source_account_registry import SourceAccountRegistry

AUGUST = CollectionMonth(2026, 8)
NOW = datetime(2026, 9, 29, 10, 30, tzinfo=UTC)
NAMED = "someone-else@nyayalabs.example"
SLACK_NAME = "2026-08_Slack_652.50-USD.pdf"


class GoogleServices:
    """Stands in for Drive and Sheets, remembering each sign-in they were built with."""

    def __init__(self) -> None:
        self.drive = FakeDrive()
        self.signed_in: list[Credentials] = []

    def __call__(self, credentials: Credentials) -> tuple[Any, Any]:
        self.signed_in.append(credentials)
        return self.drive, FakeSheets(self.drive)


def make_owner(ledger_path: Path, address: str = OWNER) -> None:
    """Connects the address and chooses it as the owner account, as the screen does."""
    SourceAccountRegistry(ledger_path).signed_in(
        address, "connected", FINANCE, NOW, signed_in_at=None, fingerprint=None, owner=True
    )


def open_dashboard(
    tmp_path: Path, services: GoogleServices, named: str | None, **more: Any
) -> TestClient:
    ledger_path = tmp_path / "ledger.sqlite"
    settings = Settings(
        session_secret="a-secret-only-for-tests",
        allowlist=frozenset({FINANCE}),
        ledger_path=ledger_path,
        token_dir=tmp_path / "tokens",
    )
    app = create_app(
        settings,
        lambda: Ledger(ledger_path),
        FakeIdentityVerifier({"code-finance": FINANCE}),
        exchange_rates=FakeExchangeRates({"USD": Decimal("95.34")}),
        owner_drive=GoogleOwnerDrive(ledger_path, settings.token_dir, named, services),
        now=lambda: NOW,
        **more,
    )
    client = TestClient(app, base_url="http://localhost:8000", follow_redirects=False)
    sign_in(client)
    return client


def owner_on_screen(dashboard: TestClient) -> dict[str, Any] | None:
    response = dashboard.get("/api/source-accounts")
    assert response.status_code == 200
    return response.json()["owner"]


# Through the dashboard's API


def test_approvals_go_to_the_drive_of_the_owner_chosen_on_the_screen(
    collection: Collection,
) -> None:
    email = hold_slack(collection)
    make_owner(collection.tmp_path / "ledger.sqlite")
    store_owner_sign_in(collection.tmp_path / "tokens")
    services = GoogleServices()

    with open_dashboard(collection.tmp_path, services, named=NAMED) as dashboard:
        owner = owner_on_screen(dashboard)
        response = approve(dashboard, email)

    assert owner is not None
    assert (owner["address"], owner["chosen_on"], owner["connected"]) == (
        OWNER,
        "source_accounts_screen",
        True,
    )
    assert (owner["drive_reached"], owner["problem"]) == (True, None)
    # The setting names another address; the screen's choice wins, and the screen says so.
    assert NAMED in owner["set_aside"] and "is used" in owner["set_aside"]
    assert response.status_code == 200
    assert response.json()["warnings"] == []
    [filed] = services.drive.named(SLACK_NAME)
    assert filed.content == SLACK_PDF
    [credentials] = services.signed_in
    assert set(credentials.scopes or ()) == set(DRIVE_SCOPES)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]


def test_an_owner_named_by_the_setting_and_not_signed_in_is_said_and_approvals_are_filed_here(
    collection: Collection,
) -> None:
    email = hold_slack(collection)
    services = GoogleServices()

    with open_dashboard(collection.tmp_path, services, named=OWNER) as dashboard:
        owner = owner_on_screen(dashboard)
        response = approve(dashboard, email)
        [row] = summary_rows(dashboard)

    assert owner is not None
    assert (owner["address"], owner["chosen_on"], owner["connected"]) == (OWNER, "setting", False)
    assert owner["drive_reached"] is False
    assert f"The owner account {OWNER} is not signed in to Google Drive" in owner["problem"]
    assert "Connect it on the Source accounts screen as the owner account" in owner["problem"]
    assert "filed on this machine only" in owner["problem"]
    assert owner["set_aside"] is None
    # The approval is filed here, as with no owner account, and the person is told why.
    assert response.status_code == 200
    [warning] = response.json()["warnings"]
    assert warning.startswith(f"Google Drive was not reached: The owner account {OWNER} is not")
    assert "filed on this machine only" in warning
    assert not row["file_url"].startswith("https://")
    assert services.signed_in == []


def test_an_owner_signed_in_on_the_screen_counts_without_a_restart(
    collection: Collection,
) -> None:
    email = hold_slack(collection)
    services = GoogleServices()

    with open_dashboard(collection.tmp_path, services, named=None) as dashboard:
        assert owner_on_screen(dashboard) is None

        # Connected and chosen on the screen while the dashboard runs.
        make_owner(collection.tmp_path / "ledger.sqlite")
        not_yet = owner_on_screen(dashboard)
        store_owner_sign_in(collection.tmp_path / "tokens")
        signed_in = owner_on_screen(dashboard)
        response = approve(dashboard, email)

    assert not_yet is not None and not_yet["drive_reached"] is False
    assert "Renew its sign-in on the Source accounts screen" in not_yet["problem"]
    assert signed_in is not None and signed_in["drive_reached"] is True
    assert response.json()["warnings"] == []
    assert services.drive.named(SLACK_NAME)


@pytest.fixture
def extractor() -> CountingExtractor:
    return CountingExtractor({ZOOM_PDF: ZOOM})


def test_an_upload_while_the_owner_is_not_signed_in_is_filed_here_with_a_warning(
    collection: Collection, extractor: CountingExtractor
) -> None:
    [email] = flag_zoom(collection)
    make_owner(collection.tmp_path / "ledger.sqlite")
    services = GoogleServices()

    with open_dashboard(
        collection.tmp_path, services, named=None, extractor=extractor
    ) as dashboard:
        response = upload(dashboard, email, ZOOM_PDF)

    assert response.status_code == 200
    assert response.json()["outcome"] == "collected"
    [warning] = response.json()["warnings"]
    assert warning.startswith("Google Drive was not reached")
    assert collection.saved_files() == ["2026-08_Zoom_149.90-USD.pdf"]
    assert services.signed_in == []


def test_an_upload_to_the_owner_signed_in_carries_no_warning(
    collection: Collection, extractor: CountingExtractor
) -> None:
    [email] = flag_zoom(collection)
    make_owner(collection.tmp_path / "ledger.sqlite")
    store_owner_sign_in(collection.tmp_path / "tokens")
    services = GoogleServices()

    with open_dashboard(
        collection.tmp_path, services, named=None, extractor=extractor
    ) as dashboard:
        response = upload(dashboard, email, ZOOM_PDF)

    assert response.json()["warnings"] == []
    assert services.drive.named("2026-08_Zoom_149.90-USD.pdf")


# Through the runner


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        session_secret="a-secret-only-for-tests",
        allowlist=frozenset({FINANCE}),
        ledger_path=tmp_path / "out" / "ledger.sqlite",
        token_dir=tmp_path / "tokens",
    )


def test_a_run_uses_the_owner_chosen_on_the_screen_over_the_one_it_was_named(
    settings: Settings,
) -> None:
    runner = CollectionRunner(settings.ledger_path, settings.token_dir, google_owner=NAMED)

    before = runner.arguments(AUGUST, None)
    make_owner(settings.ledger_path)
    after = runner.arguments(AUGUST, None)

    assert before[before.index("--google-owner") + 1] == NAMED
    assert after[after.index("--google-owner") + 1] == OWNER


@pytest.fixture
def digest(monkeypatch: pytest.MonkeyPatch) -> FakeDigestSender:
    monkeypatch.setenv("INVOICE_COLLECTOR_SLACK_WEBHOOK", "https://hooks.slack.com/services/x")
    return FakeDigestSender()


def runner_for(settings: Settings, digest: FakeDigestSender) -> CollectionRunner:
    run = partial(
        run_collection,
        google_services=GoogleServices(),
        digest_sender_for=lambda url: digest,
        mail_source_for=Mailboxes(),
        browser=no_browser,
    )
    options = "--classifier rules --vendor-matcher rules --no-exchange-rates"
    return CollectionRunner(
        settings.ledger_path, settings.token_dir, options=parse_run_options(options), run=run
    )


def test_a_run_whose_owner_is_not_signed_in_says_so_on_the_runs_screen_and_in_the_digest(
    settings: Settings, digest: FakeDigestSender
) -> None:
    make_owner(settings.ledger_path)

    with open_runs_dashboard(settings, runner_for(settings, digest), Clock()) as dashboard:
        assert dashboard.post(RUNS, json={}).status_code == 202
        answer = runs_when_done(dashboard)

    assert answer["runs"] == []
    [refused] = answer["not_started"]
    assert refused["problem"].startswith(
        f"The owner account {OWNER} is not signed in to Google Drive, or its sign-in no "
        "longer works. Renew its sign-in on the Source accounts screen"
    )
    assert refused["problem"].endswith("Nothing was collected, since a run files to its Drive.")
    [message] = digest.sent
    assert f"failed: the owner account {OWNER} is not signed in to Google Drive" in message["text"]


def test_a_scheduled_run_whose_owner_is_not_signed_in_stops_saying_why(
    settings: Settings, digest: FakeDigestSender
) -> None:
    make_owner(settings.ledger_path)

    with pytest.raises(OwnerNotSignedIn, match=f"The owner account {OWNER} is not signed in"):
        runner_for(settings, digest)(AUGUST, None, started_by="schedule")

    assert len(digest.sent) == 1


def test_the_runner_starts_when_the_owner_account_is_not_signed_in(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    served: list[object] = []

    exit_code = runner_serve.main(
        ["--ledger", str(tmp_path / "ledger.sqlite")],
        environment={
            "INVOICE_COLLECTOR_RUNNER_SECRET": "a-secret-only-for-tests",
            "INVOICE_COLLECTOR_GOOGLE_OWNER": OWNER,
            "INVOICE_COLLECTOR_TOKEN_DIR": str(tmp_path / "tokens"),
        },
        serve=lambda app, host, port: served.append(app),
    )

    said = capsys.readouterr().err
    assert exit_code == 0
    assert len(served) == 1
    assert f"Warning: The owner account {OWNER} is not signed in to Google Drive" in said
    assert "each run stops before collecting" in said


def test_the_runner_says_when_the_setting_names_another_owner(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make_owner(tmp_path / "ledger.sqlite")
    store_owner_sign_in(tmp_path / "tokens")

    exit_code = runner_serve.main(
        ["--ledger", str(tmp_path / "ledger.sqlite"), "--google-owner", NAMED],
        environment={
            "INVOICE_COLLECTOR_RUNNER_SECRET": "a-secret-only-for-tests",
            "INVOICE_COLLECTOR_TOKEN_DIR": str(tmp_path / "tokens"),
        },
        serve=lambda app, host, port: None,
    )

    said = capsys.readouterr().err
    assert exit_code == 0
    assert f"names {NAMED}, but {OWNER} is the owner account chosen" in said
    assert "not signed in" not in said
