"""The dashboard service serves the built front end, so pages and API share one origin.

Given the folder `npm run build` writes, the service answers every address that is not
the API or sign-in with the page, since the front end chooses its screen from the address.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from invoice_collector.api.app import create_app
from invoice_collector.api.identity import FakeIdentityVerifier
from invoice_collector.api.settings import Settings, SettingsError
from invoice_collector.ledger import Ledger

FINANCE = "finance@nyayalabs.example"
PAGE = "<!doctype html><title>Invoice Collection</title><div id=root></div>"
SCRIPT = "console.log('the dashboard')"


@pytest.fixture
def built(tmp_path: Path) -> Path:
    folder = tmp_path / "dist"
    (folder / "assets").mkdir(parents=True)
    (folder / "index.html").write_text(PAGE, encoding="utf-8")
    (folder / "assets" / "index-1234.js").write_text(SCRIPT, encoding="utf-8")
    (tmp_path / "secret.txt").write_text("not to be served", encoding="utf-8")
    return folder


@pytest.fixture
def ledger_path(tmp_path: Path) -> Path:
    return tmp_path / "out" / "ledger.sqlite"


def settings_for(ledger_path: Path, frontend_dir: Path | None) -> Settings:
    return Settings(
        session_secret="a-secret-only-for-tests",
        allowlist=frozenset({FINANCE}),
        ledger_path=ledger_path,
        frontend_dir=frontend_dir,
    )


@pytest.fixture
def dashboard(ledger_path: Path, built: Path) -> Iterator[TestClient]:
    app = create_app(
        settings_for(ledger_path, built),
        lambda: Ledger(ledger_path),
        FakeIdentityVerifier({"code-finance": FINANCE}),
    )
    with TestClient(app, base_url="http://localhost:8000", follow_redirects=False) as client:
        yield client


def test_the_page_is_served_at_the_root(dashboard: TestClient) -> None:
    response = dashboard.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.text == PAGE


@pytest.mark.parametrize("path", ["/summary", "/review", "/documents/abc123"])
def test_each_screen_address_is_answered_with_the_page(dashboard: TestClient, path: str) -> None:
    response = dashboard.get(path, params={"month": "2026-08"})

    assert response.status_code == 200
    assert response.text == PAGE


def test_the_built_files_are_served(dashboard: TestClient) -> None:
    response = dashboard.get("/assets/index-1234.js")

    assert response.status_code == 200
    assert response.text == SCRIPT
    assert "javascript" in response.headers["content-type"]


def test_a_missing_built_file_is_not_answered_with_the_page(dashboard: TestClient) -> None:
    assert dashboard.get("/assets/index-gone.js").status_code == 404


def test_the_api_still_needs_a_sign_in(dashboard: TestClient) -> None:
    assert dashboard.get("/api/me").status_code == 401
    assert dashboard.get("/api/no-such-thing").status_code == 401


def test_sign_in_still_goes_to_google(dashboard: TestClient) -> None:
    response = dashboard.get("/auth/login")

    assert response.status_code in (302, 307)
    assert response.headers["location"].startswith("https://accounts.google.example/")


@pytest.mark.parametrize(
    "path", ["/../secret.txt", "/%2e%2e/secret.txt", "/assets/../../secret.txt"]
)
def test_nothing_outside_the_folder_is_served(dashboard: TestClient, path: str) -> None:
    response = dashboard.get(path)

    assert "not to be served" not in response.text


def test_without_the_folder_nothing_but_the_api_is_served(ledger_path: Path) -> None:
    app = create_app(
        settings_for(ledger_path, None),
        lambda: Ledger(ledger_path),
        FakeIdentityVerifier({}),
    )
    with TestClient(app, base_url="http://localhost:8000") as client:
        assert client.get("/").status_code == 404
        assert client.get("/summary").status_code == 404


def test_a_folder_without_a_built_page_is_refused(ledger_path: Path, tmp_path: Path) -> None:
    with pytest.raises(SettingsError, match="npm run build"):
        settings_for(ledger_path, tmp_path / "not-built").check()


def test_the_folder_is_read_from_the_environment(ledger_path: Path, built: Path) -> None:
    settings = Settings.from_environment(
        {
            "INVOICE_COLLECTOR_SESSION_SECRET": "a-secret-only-for-tests",
            "INVOICE_COLLECTOR_ALLOWLIST": FINANCE,
            "INVOICE_COLLECTOR_FRONTEND_DIR": str(built),
        },
        ledger_path=ledger_path,
    )

    assert settings.frontend_dir == built
    # Pages and API share an origin, so sign-in comes back to the service itself.
    assert settings.frontend_origin == "http://localhost:8000"


def test_without_the_folder_the_front_end_is_the_development_server(ledger_path: Path) -> None:
    settings = Settings.from_environment(
        {
            "INVOICE_COLLECTOR_SESSION_SECRET": "a-secret-only-for-tests",
            "INVOICE_COLLECTOR_ALLOWLIST": FINANCE,
        },
        ledger_path=ledger_path,
    )

    assert settings.frontend_dir is None
    assert settings.frontend_origin == "http://localhost:5173"
