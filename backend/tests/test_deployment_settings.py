"""The settings a deployment gives the dashboard and the runner, and what they do with them.

Each service reads every setting from the environment, names every problem with them at
once, and refuses to start. The public address decides where Google sends a person back,
where the front end is, whether cookies are secure, and what the digest links to.
"""

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_api import FINANCE, sign_in

from invoice_collector.api.app import create_app
from invoice_collector.api.identity import FakeIdentityVerifier, WebClient
from invoice_collector.api.serve import main as dashboard_main
from invoice_collector.api.settings import Settings, SettingsError
from invoice_collector.ledger import Ledger
from invoice_collector.runner import serve as runner_serve

SECRET = "a-secret-only-for-tests"


@pytest.fixture
def ledger_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "ledger.sqlite"


def settings_from(environment: dict[str, str], ledger_path: Path) -> Settings:
    return Settings.from_environment(
        {
            "INVOICE_COLLECTOR_SESSION_SECRET": SECRET,
            "INVOICE_COLLECTOR_ALLOWLIST": FINANCE,
            **environment,
        },
        ledger_path=ledger_path,
    )


# The public address


def test_sign_in_comes_back_to_the_local_dashboard_by_default(ledger_path: Path) -> None:
    settings = settings_from({}, ledger_path)

    assert settings.public_url == "http://localhost:8000"
    assert settings.redirect_uri == "http://localhost:8000/auth/callback"
    assert settings.accounts_redirect_uri == "http://localhost:8000/accounts/callback"
    assert not settings.secure_cookies


def test_the_public_address_decides_where_google_sends_people_back(
    ledger_path: Path, tmp_path: Path
) -> None:
    built = tmp_path / "dist"
    built.mkdir()
    (built / "index.html").write_text("<!doctype html>", encoding="utf-8")

    settings = settings_from(
        {
            "INVOICE_COLLECTOR_PUBLIC_URL": "https://invoices.example.com/",
            "INVOICE_COLLECTOR_FRONTEND_DIR": str(built),
        },
        ledger_path,
    )

    assert settings.redirect_uri == "https://invoices.example.com/auth/callback"
    assert settings.accounts_redirect_uri == "https://invoices.example.com/accounts/callback"
    assert settings.frontend_origin == "https://invoices.example.com"
    assert settings.secure_cookies


@pytest.mark.parametrize(
    "address",
    [
        "invoices.example.com",
        "ftp://invoices.example.com",
        "https://x.example/dashboard",
        "http://",
    ],
)
def test_a_public_address_that_is_not_one_is_refused(ledger_path: Path, address: str) -> None:
    with pytest.raises(SettingsError, match="INVOICE_COLLECTOR_PUBLIC_URL must be the address"):
        settings_from({"INVOICE_COLLECTOR_PUBLIC_URL": address}, ledger_path)


def test_the_dashboard_listens_where_it_is_told(ledger_path: Path) -> None:
    settings = settings_from(
        {"INVOICE_COLLECTOR_DASHBOARD_HOST": "0.0.0.0", "INVOICE_COLLECTOR_DASHBOARD_PORT": "9000"},
        ledger_path,
    )

    assert (settings.host, settings.port) == ("0.0.0.0", 9000)


def test_the_dashboard_listens_on_this_machine_alone_unless_told(ledger_path: Path) -> None:
    settings = settings_from({}, ledger_path)

    assert (settings.host, settings.port) == ("127.0.0.1", 8000)


# The OAuth client


def test_the_oauth_client_is_read_from_two_settings() -> None:
    client = WebClient.from_environment(
        {
            "INVOICE_COLLECTOR_WEB_CLIENT_ID": "made-up.apps.googleusercontent.com",
            "INVOICE_COLLECTOR_WEB_CLIENT_SECRET": "made-up-value",
        }
    )

    assert client.client_id == "made-up.apps.googleusercontent.com"
    assert client.client_secret == "made-up-value"
    assert "made-up-value" not in repr(client)


def test_the_two_settings_are_preferred_to_the_file(tmp_path: Path) -> None:
    file = tmp_path / "web-client.json"
    file.write_text('{"web": {"client_id": "from-file", "client_secret": "x"}}', encoding="utf-8")

    client = WebClient.from_environment(
        {
            "INVOICE_COLLECTOR_WEB_CLIENT_FILE": str(file),
            "INVOICE_COLLECTOR_WEB_CLIENT_ID": "from-settings",
            "INVOICE_COLLECTOR_WEB_CLIENT_SECRET": "made-up-value",
        }
    )

    assert client.client_id == "from-settings"


def test_without_the_two_settings_the_file_is_read(tmp_path: Path) -> None:
    file = tmp_path / "web-client.json"
    file.write_text('{"web": {"client_id": "from-file", "client_secret": "x"}}', encoding="utf-8")

    client = WebClient.from_environment({"INVOICE_COLLECTOR_WEB_CLIENT_FILE": str(file)})

    assert client.client_id == "from-file"


def test_one_of_the_two_settings_alone_is_refused() -> None:
    with pytest.raises(SettingsError, match="INVOICE_COLLECTOR_WEB_CLIENT_SECRET is not"):
        WebClient.from_environment({"INVOICE_COLLECTOR_WEB_CLIENT_ID": "made-up"})


# Refusing to start


def served_nothing(app: FastAPI, host: str, port: int) -> None:
    raise AssertionError("the dashboard should not have started")


def test_the_dashboard_names_every_missing_setting_at_once(
    tmp_path: Path, ledger_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = dashboard_main(
        ["--ledger", str(ledger_path)],
        environment={
            "INVOICE_COLLECTOR_WEB_CLIENT_FILE": str(tmp_path / "absent.json"),
            "INVOICE_COLLECTOR_PUBLIC_URL": "not an address",
            "INVOICE_COLLECTOR_DASHBOARD_PORT": "eighty",
        },
        serve=served_nothing,
    )

    said = capsys.readouterr().err
    assert exit_code == 2
    assert "The dashboard cannot start, for 5 reasons:" in said
    assert "- INVOICE_COLLECTOR_SESSION_SECRET is not set" in said
    assert "- No OAuth client is set: INVOICE_COLLECTOR_WEB_CLIENT_ID" in said
    assert "- INVOICE_COLLECTOR_ALLOWLIST is empty and nobody is on the people list" in said
    assert "- INVOICE_COLLECTOR_PUBLIC_URL must be the address" in said
    assert "- INVOICE_COLLECTOR_DASHBOARD_PORT must be a port number" in said


def test_the_dashboard_refuses_a_ledger_folder_it_cannot_create(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "a-file").write_text("not a folder", encoding="utf-8")

    exit_code = dashboard_main(
        ["--ledger", str(tmp_path / "a-file" / "ledger.sqlite")],
        environment=dashboard_environment(),
        serve=served_nothing,
    )

    assert exit_code == 2
    assert "The ledger's folder" in capsys.readouterr().err


def dashboard_environment(**more: str) -> dict[str, str]:
    return {
        "INVOICE_COLLECTOR_SESSION_SECRET": SECRET,
        "INVOICE_COLLECTOR_ALLOWLIST": FINANCE,
        "INVOICE_COLLECTOR_WEB_CLIENT_ID": "made-up.apps.googleusercontent.com",
        "INVOICE_COLLECTOR_WEB_CLIENT_SECRET": "made-up-value",
        **more,
    }


def test_the_dashboard_starts_where_it_is_told_to_listen(ledger_path: Path) -> None:
    served: list[tuple[str, int]] = []

    exit_code = dashboard_main(
        ["--ledger", str(ledger_path)],
        environment=dashboard_environment(
            INVOICE_COLLECTOR_DASHBOARD_HOST="0.0.0.0", INVOICE_COLLECTOR_DASHBOARD_PORT="8080"
        ),
        serve=lambda app, host, port: served.append((host, port)),
    )

    assert exit_code == 0
    assert served == [("0.0.0.0", 8080)]


def test_the_owner_account_can_be_given_as_a_setting(
    ledger_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    owner = "owner@example.com"

    exit_code = dashboard_main(
        ["--ledger", str(ledger_path)],
        environment=dashboard_environment(
            INVOICE_COLLECTOR_GOOGLE_OWNER=owner,
            INVOICE_COLLECTOR_TOKEN_DIR=str(ledger_path.parent / "tokens"),
        ),
        serve=served_nothing,
    )

    # Not signed in to Drive here, which the dashboard says, naming the owner.
    assert exit_code == 1
    assert f"The owner account {owner} is not signed in" in capsys.readouterr().err


def test_the_runner_names_every_missing_setting_at_once(
    ledger_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = runner_serve.main(
        ["--ledger", str(ledger_path.parent / "other.sqlite")],
        environment={
            "INVOICE_COLLECTOR_RUNNER_PORT": "0",
            "INVOICE_COLLECTOR_PUBLIC_URL": "invoices.example.com",
        },
        serve=lambda app, host, port: None,
    )

    said = capsys.readouterr().err
    assert exit_code == 2
    assert "The runner cannot start, for 4 reasons:" in said
    assert "- INVOICE_COLLECTOR_RUNNER_SECRET is not set" in said
    assert "- INVOICE_COLLECTOR_RUNNER_PORT must be a port number" in said
    assert "- INVOICE_COLLECTOR_PUBLIC_URL must be the address" in said
    assert "- The ledger must be named ledger.sqlite" in said


def test_the_health_route_answers_without_signing_in_and_names_nobody(ledger_path: Path) -> None:
    response = dashboard(ledger_path).get("/health")

    assert response.status_code == 200
    assert response.json() == {"up": True}
    assert "set-cookie" not in response.headers


# Cookies and requests from other sites


def dashboard(ledger_path: Path, public_url: str = "http://localhost:8000") -> TestClient:
    settings = Settings(
        session_secret=SECRET,
        allowlist=frozenset({FINANCE}),
        ledger_path=ledger_path,
        public_url=public_url,
        frontend_origin=public_url,
    )
    verifier = FakeIdentityVerifier({"code-finance": FINANCE})
    app = create_app(settings, lambda: Ledger(ledger_path), verifier)
    return TestClient(app, base_url=public_url, follow_redirects=False)


def cookie_flags(response: Any) -> set[str]:
    cookie = response.headers["set-cookie"]
    return {part.strip().split("=")[0].lower() for part in cookie.split(";")[1:]}


def test_the_session_cookie_is_http_only_and_same_site_lax(ledger_path: Path) -> None:
    response = dashboard(ledger_path).get("/auth/login")

    flags = cookie_flags(response)
    assert {"httponly", "samesite"} <= flags
    assert "samesite=lax" in response.headers["set-cookie"].lower()
    assert "secure" not in flags


def test_the_session_cookie_is_secure_when_the_dashboard_is_opened_over_https(
    ledger_path: Path,
) -> None:
    response = dashboard(ledger_path, "https://invoices.example.com").get("/auth/login")

    assert {"httponly", "samesite", "secure"} <= cookie_flags(response)


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://attacker.example"},
        {"Origin": "null"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
    ],
    ids=["another origin", "no origin", "cross-site", "another origin on the same site"],
)
def test_a_change_asked_for_from_another_site_is_refused(
    ledger_path: Path, headers: dict[str, str]
) -> None:
    client = dashboard(ledger_path)
    sign_in(client)
    for method, path in [
        ("POST", "/api/vendors"),
        ("PUT", "/api/settings"),
        ("DELETE", "/api/people/someone@example.com"),
        ("POST", "/auth/logout"),
    ]:
        response = client.request(method, path, headers=headers, json={})

        assert response.status_code == 403, (method, path)
        assert response.json() == {"detail": "Changes are made from the dashboard's own pages only"}


def test_a_change_from_the_dashboards_own_pages_goes_through(ledger_path: Path) -> None:
    client = dashboard(ledger_path)
    sign_in(client)

    response = client.post(
        "/auth/logout",
        headers={"Origin": "http://localhost:8000", "Sec-Fetch-Site": "same-origin"},
    )

    assert response.status_code == 204
    assert client.get("/api/me").status_code == 401


def test_a_request_that_does_not_say_where_it_comes_from_is_left_to_the_routes(
    ledger_path: Path,
) -> None:
    # Not from a page in a browser, so it carries nobody's cookie by surprise; the route's
    # own check still refuses it, as nobody is signed in.
    response = dashboard(ledger_path).post("/api/vendors", json={})

    assert response.status_code == 401


def test_reading_from_another_site_is_not_refused_here(ledger_path: Path) -> None:
    # Reading changes nothing; the browser keeps the answer from the other site (CORS).
    response = dashboard(ledger_path).get("/api/me", headers={"Origin": "https://attacker.example"})

    assert response.status_code == 401
