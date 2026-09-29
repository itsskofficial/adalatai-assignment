"""The run the dashboard starts: the collect command's run, with its options.

Run through the dashboard's API against sample mail, with fakes for Google, Claude, Slack
and the browser.
"""

from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from functools import partial
from pathlib import Path

import pytest
from conftest import ReplayClient
from fake_google import FakeDrive, FakeSheets
from test_api import AUGUST, ENGINEERING, FINANCE
from test_api_runs import RUNS, Clock, connect, open_dashboard, runs_when_done
from test_cli import OWNER, SLACK_ANSWER, Mailboxes, store_owner_sign_in

from invoice_collector.api.collection_runner import (
    CollectionRunner,
    RunOptionsRefused,
    parse_run_options,
)
from invoice_collector.api.serve import main as serve_main
from invoice_collector.api.settings import Settings
from invoice_collector.cli import Browser, run_collection
from invoice_collector.destinations import DestinationPolicy
from invoice_collector.digest import FakeDigestSender
from invoice_collector.ledger import Ledger
from invoice_collector.mail_source import MailSource
from invoice_collector.portal import LoginGated


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        session_secret="a-secret-only-for-tests",
        allowlist=frozenset({FINANCE}),
        ledger_path=tmp_path / "out" / "ledger.sqlite",
        token_dir=tmp_path / "tokens",
    )


class NoBrowser:
    def render_html(self, html: str) -> bytes:
        raise AssertionError("no email in these tests needs a browser")

    def fetch(self, url: str) -> bytes | LoginGated:
        raise AssertionError("no email in these tests needs a browser")


def no_browser(policy: DestinationPolicy) -> AbstractContextManager[Browser]:
    return nullcontext(NoBrowser())


class SwitchableMailboxes:
    """Mailboxes whose sign-ins can be mended between runs."""

    def __init__(self, not_signed_in: list[str]) -> None:
        self.now = Mailboxes(not_signed_in)

    def __call__(self, account: str, token_dir: Path) -> MailSource:
        return self.now(account, token_dir)


def real_runner(
    settings: Settings,
    replay_client: ReplayClient,
    mailboxes: Callable[[str, Path], MailSource],
    digest: FakeDigestSender,
    drive: FakeDrive,
) -> CollectionRunner:
    run = partial(
        run_collection,
        google_services=lambda credentials: (drive, FakeSheets(drive)),
        digest_sender_for=lambda url: digest,
        mail_source_for=mailboxes,
        claude_client=lambda: replay_client(200, SLACK_ANSWER),
        browser=no_browser,
    )
    return CollectionRunner(
        settings.ledger_path,
        settings.token_dir,
        google_owner=OWNER,
        options=parse_run_options("--classifier rules --vendor-matcher rules --no-exchange-rates"),
        run=run,
    )


def test_a_run_from_the_dashboard_is_the_collect_commands_run_and_a_failed_account_is_run_again(
    settings: Settings,
    replay_client: ReplayClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INVOICE_COLLECTOR_SLACK_WEBHOOK", "https://hooks.slack.com/services/x")
    store_owner_sign_in(settings.token_dir)
    connect(settings, ENGINEERING, OWNER)
    mailboxes = SwitchableMailboxes(not_signed_in=[ENGINEERING])
    digest, drive = FakeDigestSender(), FakeDrive()
    runner = real_runner(settings, replay_client, mailboxes, digest, drive)

    with open_dashboard(settings, runner, Clock()) as dashboard:
        assert dashboard.post(RUNS, json={}).status_code == 202
        [first] = runs_when_done(dashboard)["runs"]
        assert (first["state"], first["started_by"], first["person"]) == (
            "finished",
            "dashboard",
            FINANCE,
        )
        assert first["collected"] == 1
        [engineering] = [s for s in first["source_accounts"] if not s["read"]]
        assert engineering["source_account"] == ENGINEERING
        assert engineering["can_run_again"] is True
        assert engineering["reason"]
        # The digest and the sheet are written as by a run from anywhere else.
        assert len(digest.sent) == 1
        assert drive.named("Invoice summary 2026-08")

        mailboxes.now = Mailboxes()
        again = dashboard.post(RUNS, json={"source_account": ENGINEERING})
        assert again.status_code == 202
        second, first_again = runs_when_done(dashboard)["runs"]

    assert second["only_source_account"] == ENGINEERING
    assert [s["source_account"] for s in second["source_accounts"]] == [ENGINEERING]
    # Only the account run again was read, and only its email was counted.
    assert second["collected"] == 1 and second["emails_found"] == 1
    assert first_again["collected"] == 1
    ledger = Ledger(settings.ledger_path)
    try:
        documents = ledger.documents(AUGUST)
        syncs = ledger.syncs(AUGUST)
    finally:
        ledger.close()
    # What the first run collected from the other account is kept (ADR 0013).
    assert sorted(d.source_account for d in documents) == [ENGINEERING, OWNER]
    assert all(s.succeeded for s in syncs)
    assert len(digest.sent) == 2


def test_the_runner_gives_the_collect_command_the_dashboards_own_options(
    settings: Settings,
) -> None:
    runner = CollectionRunner(
        settings.ledger_path,
        settings.token_dir,
        google_owner=OWNER,
        options=parse_run_options("--no-exchange-rates --map sample@x.example=real@x.example"),
    )

    assert runner.arguments(AUGUST, None) == [
        "2026-08",
        "--out",
        str(settings.ledger_path.parent),
        "--token-dir",
        str(settings.token_dir),
        "--google-owner",
        OWNER,
        "--connected-accounts",
        "--no-exchange-rates",
        "--map",
        "sample@x.example=real@x.example",
    ]
    assert runner.arguments(AUGUST, ENGINEERING)[7:9] == ["--account", ENGINEERING]


@pytest.mark.parametrize(
    "options",
    ["--samples seed", "--account a@x.example", "--out=elsewhere", "--no-such-option"],
)
def test_run_options_the_dashboard_sets_itself_or_does_not_know_are_refused(
    options: str,
) -> None:
    with pytest.raises(RunOptionsRefused):
        parse_run_options(options)


def test_the_runner_needs_the_ledger_a_run_writes(tmp_path: Path) -> None:
    with pytest.raises(RunOptionsRefused, match="ledger.sqlite"):
        CollectionRunner(tmp_path / "other.sqlite", tmp_path / "tokens")


def start_dashboard_command(tmp_path: Path, ledger_path: Path, *options: str) -> int:
    web_client_file = tmp_path / "web-client.json"
    web_client_file.write_text(
        '{"web": {"client_id": "made-up-id", "client_secret": "made-up-value"}}',
        encoding="utf-8",
    )
    return serve_main(
        ["--ledger", str(ledger_path), *options],
        environment={
            "INVOICE_COLLECTOR_SESSION_SECRET": "a-secret-only-for-tests",
            "INVOICE_COLLECTOR_ALLOWLIST": FINANCE,
            "INVOICE_COLLECTOR_WEB_CLIENT_FILE": str(web_client_file),
        },
        serve=lambda app: None,
    )


def test_dashboard_command_refuses_run_options_it_sets_itself(
    tmp_path: Path, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = start_dashboard_command(
        tmp_path, settings.ledger_path, "--run-options=--out elsewhere"
    )

    assert exit_code == 2
    assert "--out cannot be among the run options" in capsys.readouterr().err


def test_dashboard_command_warns_when_runs_cannot_write_to_its_ledger(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = start_dashboard_command(tmp_path, tmp_path / "copy.sqlite")

    assert exit_code == 0
    assert "runs cannot be started on the Runs screen" in capsys.readouterr().err
