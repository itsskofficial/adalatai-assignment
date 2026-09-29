"""The People screen's API, called the way the dashboard calls it."""

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_api import FINANCE, STRANGER, sign_in

from invoice_collector.api.app import create_app
from invoice_collector.api.identity import FakeIdentityVerifier
from invoice_collector.api.settings import Settings, SettingsError
from invoice_collector.ledger import Ledger

OPS = "ops@nyayalabs.example"
AUDIT = "audit@nyayalabs.example"
NOW = datetime(2026, 9, 29, 10, 30, tzinfo=UTC)


class Clock:
    """The time the app is told it is. Tests move it on to tell events apart."""

    def __init__(self) -> None:
        self.time = NOW

    def __call__(self) -> datetime:
        return self.time

    def tick(self, minutes: int = 1) -> None:
        self.time += timedelta(minutes=minutes)


@pytest.fixture
def ledger_path(tmp_path: Path) -> Path:
    return tmp_path / "out" / "ledger.sqlite"


@pytest.fixture
def ledger(ledger_path: Path) -> Iterator[Ledger]:
    """A ledger a collection has written, so the file exists."""
    ledger = Ledger(ledger_path)
    yield ledger
    ledger.close()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def verifier() -> FakeIdentityVerifier:
    return FakeIdentityVerifier(
        {
            "code-finance": FINANCE,
            "code-ops": OPS,
            "code-ops-shouting": "Ops@NyayaLabs.Example",
            "code-audit": AUDIT,
            "code-stranger": STRANGER,
        }
    )


AppFactory = Callable[..., FastAPI]


@pytest.fixture
def make_app(
    ledger: Ledger, ledger_path: Path, verifier: FakeIdentityVerifier, clock: Clock
) -> AppFactory:
    def make(*allowlist: str) -> FastAPI:
        settings = Settings(
            session_secret="a-secret-only-for-tests",
            allowlist=frozenset(allowlist),
            ledger_path=ledger_path,
        )
        return create_app(settings, lambda: Ledger(ledger_path), verifier, now=clock)

    return make


@pytest.fixture
def app(make_app: AppFactory) -> FastAPI:
    return make_app(FINANCE)


@pytest.fixture
def browsers(app: FastAPI) -> Iterator[Callable[[], TestClient]]:
    """Opens a new browser on the dashboard: each holds its own session."""
    opened: list[TestClient] = []

    def open_one() -> TestClient:
        client = TestClient(app, base_url="http://localhost:8000", follow_redirects=False)
        opened.append(client)
        return client

    yield open_one
    for client in opened:
        client.close()


@pytest.fixture
def administrator(browsers: Callable[[], TestClient]) -> TestClient:
    """The finance administrator, set by the installation, signed in."""
    client = browsers()
    sign_in(client, "code-finance")
    return client


def signed_in_as(browsers: Callable[[], TestClient], code: str) -> TestClient:
    client = browsers()
    sign_in(client, code)
    return client


def path_of(address: str) -> str:
    return f"/api/people/{quote(address, safe='')}"


def people(dashboard: TestClient) -> list[dict[str, Any]]:
    response = dashboard.get("/api/people")
    assert response.status_code == 200
    return response.json()


def entry_for(dashboard: TestClient, address: str) -> dict[str, Any]:
    for person in people(dashboard):
        if person["address"] == address:
            return person
    raise AssertionError(f"{address} is not on the list")


def add(dashboard: TestClient, address: str, role: str = "member") -> int:
    return dashboard.post("/api/people", json={"address": address, "role": role}).status_code


# Signing in


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/people"),
        ("POST", "/api/people"),
        ("PUT", path_of(OPS)),
        ("DELETE", path_of(OPS)),
        ("GET", "/api/people/history"),
        ("GET", "/api/people/refused"),
    ],
)
def test_people_routes_return_401_when_not_signed_in(
    browsers: Callable[[], TestClient], method: str, path: str
) -> None:
    response = browsers().request(method, path, json={"address": OPS, "role": "member"})

    assert response.status_code == 401


def test_me_gives_the_role_of_an_administrator_set_by_the_installation(
    administrator: TestClient,
) -> None:
    assert administrator.get("/api/me").json() == {"email": FINANCE, "role": "administrator"}


def test_a_person_added_in_the_dashboard_can_sign_in(
    administrator: TestClient, browsers: Callable[[], TestClient]
) -> None:
    assert add(administrator, OPS) == 201

    ops = signed_in_as(browsers, "code-ops")

    assert ops.get("/api/me").json() == {"email": OPS, "role": "member"}


def test_a_removed_person_is_refused_on_their_next_request(
    administrator: TestClient, browsers: Callable[[], TestClient]
) -> None:
    add(administrator, OPS)
    ops = signed_in_as(browsers, "code-ops")
    assert ops.get("/api/months").status_code == 200

    removed = administrator.delete(path_of(OPS))

    assert removed.status_code == 204
    assert ops.get("/api/months").status_code == 401
    assert OPS not in [person["address"] for person in people(administrator)]


# Members


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/people"),
        ("POST", "/api/people"),
        ("PUT", path_of(AUDIT)),
        ("DELETE", path_of(AUDIT)),
        ("GET", "/api/people/history"),
        ("GET", "/api/people/refused"),
    ],
)
def test_a_member_is_forbidden_every_people_route(
    administrator: TestClient,
    browsers: Callable[[], TestClient],
    method: str,
    path: str,
) -> None:
    add(administrator, OPS)
    add(administrator, AUDIT)
    ops = signed_in_as(browsers, "code-ops")

    response = ops.request(method, path, json={"address": STRANGER, "role": "administrator"})

    assert response.status_code == 403
    assert response.json() == {"detail": "Only an administrator can manage who may sign in"}
    assert {person["address"] for person in people(administrator)} == {FINANCE, OPS, AUDIT}
    assert entry_for(administrator, AUDIT)["role"] == "member"


def test_a_member_can_use_the_rest_of_the_dashboard(
    administrator: TestClient, browsers: Callable[[], TestClient]
) -> None:
    add(administrator, OPS)
    ops = signed_in_as(browsers, "code-ops")

    assert ops.get("/api/months/2026-08/summary").status_code == 200
    assert ops.get("/api/vendors").status_code == 200


# The list


def test_the_list_shows_who_added_each_person_and_when(
    administrator: TestClient, clock: Clock
) -> None:
    clock.tick()
    add(administrator, OPS)

    assert people(administrator) == [
        {
            "address": FINANCE,
            "role": "administrator",
            "set_by_installation": True,
            "added_by": None,
            "added_at": None,
            "last_signed_in_at": NOW.isoformat(),
        },
        {
            "address": OPS,
            "role": "member",
            "set_by_installation": False,
            "added_by": FINANCE,
            "added_at": (NOW + timedelta(minutes=1)).isoformat(),
            "last_signed_in_at": None,
        },
    ]


def test_each_sign_in_is_stored_as_the_last_sign_in(
    administrator: TestClient, browsers: Callable[[], TestClient], clock: Clock
) -> None:
    add(administrator, OPS)
    clock.tick(5)
    signed_in_as(browsers, "code-ops")
    clock.tick(5)
    signed_in_as(browsers, "code-ops")

    assert (
        entry_for(administrator, OPS)["last_signed_in_at"]
        == (NOW + timedelta(minutes=10)).isoformat()
    )


# Adding


def test_capitals_are_ignored(
    administrator: TestClient, browsers: Callable[[], TestClient]
) -> None:
    assert add(administrator, "  Ops@NyayaLabs.EXAMPLE ") == 201

    ops = signed_in_as(browsers, "code-ops-shouting")

    assert ops.get("/api/me").json() == {"email": OPS, "role": "member"}
    assert entry_for(administrator, OPS)["address"] == OPS
    administrator.put(path_of("OPS@nyayalabs.example"), json={"role": "administrator"})
    assert entry_for(administrator, OPS)["role"] == "administrator"


@pytest.mark.parametrize(
    "address",
    ["", "ops", "ops@", "@nyayalabs.example", "ops@nyayalabs", "o p@x.example", "a@b@c.example"],
)
def test_an_address_that_is_not_an_email_address_is_refused(
    administrator: TestClient, address: str
) -> None:
    response = administrator.post("/api/people", json={"address": address, "role": "member"})

    assert response.status_code == 422
    assert response.json() == {
        "detail": "The address must be an email address, such as name@example.com"
    }
    assert [person["address"] for person in people(administrator)] == [FINANCE]


def test_a_role_that_is_not_member_or_administrator_is_refused(
    administrator: TestClient,
) -> None:
    response = administrator.post("/api/people", json={"address": OPS, "role": "owner"})

    assert response.status_code == 422
    assert response.json() == {"detail": "The role must be member or administrator"}


def test_a_person_already_on_the_list_is_refused_in_any_capitals(
    administrator: TestClient,
) -> None:
    add(administrator, OPS)

    response = administrator.post(
        "/api/people", json={"address": "OPS@NyayaLabs.example", "role": "administrator"}
    )

    assert response.status_code == 409
    assert response.json() == {"detail": f"{OPS} is already on the list"}
    assert entry_for(administrator, OPS)["role"] == "member"


def test_an_administrator_set_by_the_installation_cannot_be_added_again(
    administrator: TestClient,
) -> None:
    response = administrator.post("/api/people", json={"address": FINANCE, "role": "member"})

    assert response.status_code == 409
    assert response.json() == {"detail": f"{FINANCE} is already on the list"}


# Changing and removing


def test_a_member_can_be_made_an_administrator(
    administrator: TestClient, browsers: Callable[[], TestClient]
) -> None:
    add(administrator, OPS)
    ops = signed_in_as(browsers, "code-ops")
    assert ops.get("/api/people").status_code == 403

    response = administrator.put(path_of(OPS), json={"role": "administrator"})

    assert response.status_code == 200
    assert response.json()["role"] == "administrator"
    assert ops.get("/api/me").json()["role"] == "administrator"
    assert ops.get("/api/people").status_code == 200


def test_an_administrator_can_be_made_a_member(administrator: TestClient) -> None:
    add(administrator, OPS, "administrator")

    response = administrator.put(path_of(OPS), json={"role": "member"})

    assert response.status_code == 200
    assert entry_for(administrator, OPS)["role"] == "member"


def test_an_administrator_set_by_the_installation_cannot_be_changed_or_removed(
    administrator: TestClient,
) -> None:
    changed = administrator.put(path_of("Finance@NyayaLabs.example"), json={"role": "member"})
    removed = administrator.delete(path_of(FINANCE))

    message = {
        "detail": f"{FINANCE} is set by the installation and cannot be changed in the dashboard"
    }
    assert (changed.status_code, changed.json()) == (409, message)
    assert (removed.status_code, removed.json()) == (409, message)
    assert entry_for(administrator, FINANCE)["role"] == "administrator"


def test_changing_or_removing_a_person_not_on_the_list_answers_404(
    administrator: TestClient,
) -> None:
    changed = administrator.put(path_of(OPS), json={"role": "administrator"})
    removed = administrator.delete(path_of(OPS))

    assert changed.status_code == removed.status_code == 404
    assert removed.json() == {"detail": f"{OPS} is not on the list"}


def test_the_last_administrator_cannot_remove_or_demote_themselves(
    make_app: AppFactory,
) -> None:
    # An installation that once had an administrator in the setting, now emptied.
    with TestClient(
        make_app(FINANCE), base_url="http://localhost:8000", follow_redirects=False
    ) as first:
        sign_in(first)
        add(first, OPS, "administrator")
        add(first, AUDIT, "member")
    with TestClient(make_app(), base_url="http://localhost:8000", follow_redirects=False) as ops:
        sign_in(ops, "code-ops")

        demoted = ops.put(path_of(OPS), json={"role": "member"})
        removed = ops.delete(path_of(OPS))

        message = {"detail": "That would leave nobody who can manage who may sign in"}
        assert (demoted.status_code, demoted.json()) == (409, message)
        assert (removed.status_code, removed.json()) == (409, message)
        assert ops.get("/api/me").json() == {"email": OPS, "role": "administrator"}
        assert ops.delete(path_of(AUDIT)).status_code == 204


def test_an_administrator_may_remove_themselves_while_another_remains(
    administrator: TestClient, browsers: Callable[[], TestClient]
) -> None:
    add(administrator, OPS, "administrator")
    ops = signed_in_as(browsers, "code-ops")

    assert ops.delete(path_of(OPS)).status_code == 204
    assert ops.get("/api/me").status_code == 401


# History


def test_additions_changes_and_removals_are_recorded_with_who_and_when(
    administrator: TestClient, browsers: Callable[[], TestClient], clock: Clock
) -> None:
    add(administrator, OPS, "administrator")
    ops = signed_in_as(browsers, "code-ops")
    clock.tick()
    add(ops, AUDIT)
    clock.tick()
    ops.put(path_of(AUDIT), json={"role": "administrator"})
    clock.tick()
    administrator.delete(path_of(AUDIT))

    response = administrator.get("/api/people/history")

    assert response.status_code == 200
    assert response.json() == [
        {
            "address": AUDIT,
            "action": "removed",
            "role_before": "administrator",
            "role_after": None,
            "person": FINANCE,
            "changed_at": (NOW + timedelta(minutes=3)).isoformat(),
        },
        {
            "address": AUDIT,
            "action": "role_changed",
            "role_before": "member",
            "role_after": "administrator",
            "person": OPS,
            "changed_at": (NOW + timedelta(minutes=2)).isoformat(),
        },
        {
            "address": AUDIT,
            "action": "added",
            "role_before": None,
            "role_after": "member",
            "person": OPS,
            "changed_at": (NOW + timedelta(minutes=1)).isoformat(),
        },
        {
            "address": OPS,
            "action": "added",
            "role_before": None,
            "role_after": "administrator",
            "person": FINANCE,
            "changed_at": NOW.isoformat(),
        },
    ]


def test_a_refused_change_is_not_recorded(administrator: TestClient) -> None:
    administrator.post("/api/people", json={"address": "not an address", "role": "member"})
    administrator.delete(path_of(FINANCE))

    assert administrator.get("/api/people/history").json() == []


# Refused sign-ins


def test_a_refused_sign_in_is_recorded_and_listed_newest_first(
    administrator: TestClient, browsers: Callable[[], TestClient], clock: Clock
) -> None:
    clock.tick()
    sign_in(browsers(), "code-stranger")
    clock.tick()
    sign_in(browsers(), "code-audit")

    response = administrator.get("/api/people/refused")

    assert response.status_code == 200
    assert response.json() == [
        {"address": AUDIT, "attempted_at": (NOW + timedelta(minutes=2)).isoformat()},
        {"address": STRANGER, "attempted_at": (NOW + timedelta(minutes=1)).isoformat()},
    ]


def test_only_the_latest_fifty_refused_sign_ins_are_listed(
    administrator: TestClient, browsers: Callable[[], TestClient], clock: Clock
) -> None:
    stranger = browsers()
    for _ in range(55):
        clock.tick()
        sign_in(stranger, "code-stranger")

    refused = administrator.get("/api/people/refused").json()

    assert len(refused) == 50
    assert refused[0]["attempted_at"] == (NOW + timedelta(minutes=55)).isoformat()
    assert refused[-1]["attempted_at"] == (NOW + timedelta(minutes=6)).isoformat()


def test_a_refused_address_can_be_added_and_then_signs_in(
    administrator: TestClient, browsers: Callable[[], TestClient]
) -> None:
    sign_in(browsers(), "code-audit")
    (attempt,) = administrator.get("/api/people/refused").json()

    add(administrator, attempt["address"])

    assert signed_in_as(browsers, "code-audit").get("/api/me").status_code == 200


# Starting


def test_the_app_refuses_to_start_when_nobody_could_sign_in(make_app: AppFactory) -> None:
    with pytest.raises(SettingsError, match="nobody could sign in"):
        make_app()


def test_the_app_starts_with_an_empty_setting_when_the_list_holds_an_administrator(
    make_app: AppFactory,
) -> None:
    with TestClient(
        make_app(FINANCE), base_url="http://localhost:8000", follow_redirects=False
    ) as first:
        sign_in(first)
        add(first, OPS, "administrator")

    with TestClient(
        make_app(), base_url="http://localhost:8000", follow_redirects=False
    ) as dashboard:
        sign_in(dashboard, "code-ops")
        assert dashboard.get("/api/me").json() == {"email": OPS, "role": "administrator"}
        sign_in(dashboard, "code-finance")
        assert dashboard.get("/api/me").status_code == 401
