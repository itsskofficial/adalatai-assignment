"""Sample mail whose links were written for the sample portal at another address.

Sample mail put into a mailbox with the seed command has its portal links written for
http://localhost:8765, the generator's own address, while in Compose the runner reaches the
sample portal at http://portal:8765. Gmail cannot change a message, so those links stay.
INVOICE_COLLECTOR_SAMPLE_PORTAL_LINKS names the address they were written with, and a link
written with exactly that address is opened at the sample portal's, and nothing else is.
"""

from collections.abc import Iterator
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from functools import partial
from pathlib import Path

import pytest
from support import ENGINEERING, portal_email, real_pdf
from test_browser import INVOICE, SIGN_IN, Site

from invoice_collector.api.collection_runner import CollectionRunner, parse_run_options
from invoice_collector.api.review import review_queue
from invoice_collector.api.serve import main as dashboard_main
from invoice_collector.api.settings import Settings, SettingsError
from invoice_collector.browser import Browser, HeadlessBrowser
from invoice_collector.cli import main as collect_main
from invoice_collector.cli import run_collection
from invoice_collector.destinations import DestinationPolicy, NotAnOrigin
from invoice_collector.domain import CollectionMonth, EmailState, Extraction
from invoice_collector.extractor import NO_HINTS, Hints, content_hash
from invoice_collector.ledger import Ledger
from invoice_collector.mail_source import InMemoryMailSource, MailSource
from invoice_collector.portal import LoginGated, OpenedWhereTheyLead, PortalFetchFailed
from invoice_collector.runner import serve as runner_serve

AUGUST = CollectionMonth(2026, 8)
PORTAL = "http://portal:8765"
WRITTEN = "http://localhost:8765"
SETTINGS = {
    "INVOICE_COLLECTOR_SAMPLE_PORTAL_URL": PORTAL,
    "INVOICE_COLLECTOR_SAMPLE_PORTAL_LINKS": WRITTEN,
}
FIGMA_PDF = real_pdf("Figma invoice")

TOKENISED = f"{WRITTEN}/in_1PqX7fK2.html?download=1"
SIGN_IN_PAGE = f"{WRITTEN}/sign-in.html"
ANOTHER_PORT = "http://localhost:8766/in_9ZzQ.html"
WITH_A_USER = "http://finance@localhost:8765/in_4Rt.html"


# Through a run, with a fetcher that records what it was asked to open


@dataclass
class RecordingBrowser:
    """Checks each address against the policy, as the real browser does, and records it."""

    policy: DestinationPolicy
    pages: dict[str, bytes | LoginGated]
    asked: list[str] = field(default_factory=list[str])

    def render_html(self, html: str) -> bytes:
        raise AssertionError("no email in these tests needs its body rendered")

    def fetch(self, url: str) -> bytes | LoginGated:
        self.asked.append(url)
        refusal = self.policy.refusal(url)
        if refusal:
            raise PortalFetchFailed(f"could not open {url}: {refusal}")
        try:
            return self.pages[url]
        except KeyError:
            raise PortalFetchFailed(f"could not open {url}: HTTP 404") from None


class FigmaReader:
    def extract(self, pdf: bytes, hints: Hints = NO_HINTS) -> Extraction:
        return Extraction("invoice", "Figma", datetime(2026, 8, 3).date(), Decimal("45.00"), "USD")


def emails() -> MailSource:
    at = datetime(2026, 8, 3, 9, tzinfo=UTC)
    return InMemoryMailSource(
        ENGINEERING,
        [
            portal_email("Figma", TOKENISED, received=at),
            portal_email("Zoom", SIGN_IN_PAGE, received=at.replace(hour=10)),
            portal_email("Notion", ANOTHER_PORT, received=at.replace(hour=11)),
            portal_email("Miro", WITH_A_USER, received=at.replace(hour=12)),
        ],
    )


@pytest.fixture
def opened() -> list[RecordingBrowser]:
    return []


def run_august(tmp_path: Path, opened: list[RecordingBrowser]) -> Path:
    """Runs August as the runner and the dashboard run it, reading the one mailbox."""
    pages: dict[str, bytes | LoginGated] = {
        f"{PORTAL}/in_1PqX7fK2.html?download=1": FIGMA_PDF,
        f"{PORTAL}/sign-in.html": LoginGated(),
    }

    def browser(policy: DestinationPolicy) -> AbstractContextManager[Browser]:
        opened.append(RecordingBrowser(policy, pages))
        return nullcontext(opened[-1])

    ledger_path = tmp_path / "out" / "ledger.sqlite"
    run = partial(
        run_collection,
        mail_source_for=lambda account, token_dir: emails(),
        browser=browser,
        extractor=FigmaReader(),
    )
    options = "--classifier rules --vendor-matcher rules --no-exchange-rates --no-digest"
    runner = CollectionRunner(
        ledger_path, tmp_path / "tokens", options=parse_run_options(options), run=run
    )
    runner(AUGUST, ENGINEERING, started_by="dashboard")
    return ledger_path


def test_a_link_written_for_the_sample_portal_elsewhere_is_opened_at_the_portal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened: list[RecordingBrowser]
) -> None:
    for name, value in SETTINGS.items():
        monkeypatch.setenv(name, value)

    ledger_path = run_august(tmp_path, opened)

    [browser] = opened
    # The path and query are kept; nothing written for localhost:8765 is asked for as it is.
    assert browser.asked[:2] == [
        f"{PORTAL}/in_1PqX7fK2.html?download=1",
        f"{PORTAL}/sign-in.html",
    ]
    # Another port of the same host, and a link carrying a user name, are asked for as
    # written, and refused.
    assert browser.asked[2:] == [ANOTHER_PORT, WITH_A_USER]
    ledger = Ledger(ledger_path)
    try:
        examined = {e.subject.split()[1]: e for e in ledger.examined_emails(AUGUST)}
        [document] = ledger.documents(AUGUST)
        queue = review_queue(ledger, ledger_path, AUGUST)
    finally:
        ledger.close()
    assert examined["Figma"].state is EmailState.COLLECTED
    assert examined["Zoom"].state is EmailState.NEEDS_REVIEW
    assert examined["Notion"].state is EmailState.FAILED
    assert "http links are not followed" in (examined["Notion"].reason or "")
    assert examined["Miro"].state is EmailState.FAILED
    # The ledger keeps each link as the email wrote it, and the document is known by it, so
    # one collected before is not read again.
    assert [examined[v].portal_link for v in ("Figma", "Zoom", "Notion", "Miro")] == [
        TOKENISED,
        SIGN_IN_PAGE,
        ANOTHER_PORT,
        WITH_A_USER,
    ]
    assert document.content_hash == content_hash(TOKENISED.encode())
    # The Review screen offers the link as the email wrote it, for a person to download.
    [waiting] = [item for item in queue.items if item.needs_manual_download]
    assert waiting.portal_link == SIGN_IN_PAGE


def test_without_the_setting_a_link_written_for_localhost_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened: list[RecordingBrowser]
) -> None:
    monkeypatch.setenv("INVOICE_COLLECTOR_SAMPLE_PORTAL_URL", PORTAL)
    monkeypatch.delenv("INVOICE_COLLECTOR_SAMPLE_PORTAL_LINKS", raising=False)

    ledger_path = run_august(tmp_path, opened)

    [browser] = opened
    assert browser.asked == [TOKENISED, SIGN_IN_PAGE, ANOTHER_PORT, WITH_A_USER]
    ledger = Ledger(ledger_path)
    try:
        states = {e.state for e in ledger.examined_emails(AUGUST)}
    finally:
        ledger.close()
    assert states == {EmailState.FAILED}


# Where a link is opened


@pytest.mark.parametrize(
    ("link", "opened_at"),
    [
        (f"{WRITTEN}/in_1PqX7fK2.html", f"{PORTAL}/in_1PqX7fK2.html"),
        (f"{WRITTEN}/in_1PqX7fK2.html?a=1#top", f"{PORTAL}/in_1PqX7fK2.html?a=1"),
        ("HTTP://LOCALHOST:8765/sign-in.html", f"{PORTAL}/sign-in.html"),
        ("http://localhost:8766/in_1.html", "http://localhost:8766/in_1.html"),
        ("https://localhost:8765/in_1.html", "https://localhost:8765/in_1.html"),
        ("http://127.0.0.1:8765/in_1.html", "http://127.0.0.1:8765/in_1.html"),
        ("http://user:pw@localhost:8765/in_1.html", "http://user:pw@localhost:8765/in_1.html"),
        ("http://@localhost:8765/in_1.html", "http://@localhost:8765/in_1.html"),
        ("http://localhost:port/in_1.html", "http://localhost:port/in_1.html"),
        ("https://billing.vendor.example/in_1", "https://billing.vendor.example/in_1"),
    ],
)
def test_only_a_link_written_with_exactly_that_address_is_opened_at_the_portal(
    link: str, opened_at: str
) -> None:
    policy = DestinationPolicy.from_environment(SETTINGS)

    assert policy.address_to_open(link) == opened_at


def test_several_addresses_may_be_named() -> None:
    policy = DestinationPolicy.from_environment(
        {
            **SETTINGS,
            "INVOICE_COLLECTOR_SAMPLE_PORTAL_LINKS": " http://localhost:8765, http://127.0.0.1:8765/",
        }
    )

    assert policy.address_to_open("http://127.0.0.1:8765/a.html") == f"{PORTAL}/a.html"
    assert policy.address_to_open("http://localhost:8765/b.html") == f"{PORTAL}/b.html"


def test_the_addresses_themselves_are_never_opened() -> None:
    policy = DestinationPolicy.from_environment(SETTINGS)

    assert policy.refusal(f"{WRITTEN}/in_1PqX7fK2.html") == "http links are not followed"
    assert policy.refusal(f"{PORTAL}/in_1PqX7fK2.html") is None


@pytest.mark.parametrize(
    "links",
    ["localhost:8765", "http://localhost:8765/samples", "ftp://localhost:21", "http://u@h:1"],
)
def test_each_address_must_be_a_scheme_a_host_and_a_port(links: str) -> None:
    with pytest.raises(NotAnOrigin, match="INVOICE_COLLECTOR_SAMPLE_PORTAL_LINKS must be"):
        DestinationPolicy.from_environment(
            {**SETTINGS, "INVOICE_COLLECTOR_SAMPLE_PORTAL_LINKS": links}
        )


# Given alone, it is named when a service starts


LINKS_ALONE = {"INVOICE_COLLECTOR_SAMPLE_PORTAL_LINKS": WRITTEN}
ALONE = "INVOICE_COLLECTOR_SAMPLE_PORTAL_LINKS is set but INVOICE_COLLECTOR_SAMPLE_PORTAL_URL"


def test_the_dashboard_names_the_setting_given_without_the_portal(tmp_path: Path) -> None:
    with pytest.raises(SettingsError, match=ALONE):
        Settings.from_environment(
            {
                "INVOICE_COLLECTOR_SESSION_SECRET": "a-secret-only-for-tests",
                "INVOICE_COLLECTOR_ALLOWLIST": "finance@nyayalabs.example",
                **LINKS_ALONE,
            },
            ledger_path=tmp_path / "ledger.sqlite",
        )


def test_the_dashboard_refuses_to_start_with_the_setting_alone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = dashboard_main(
        ["--ledger", str(tmp_path / "ledger.sqlite")],
        environment={
            "INVOICE_COLLECTOR_SESSION_SECRET": "a-secret-only-for-tests",
            "INVOICE_COLLECTOR_ALLOWLIST": "finance@nyayalabs.example",
            "INVOICE_COLLECTOR_WEB_CLIENT_ID": "made-up.apps.googleusercontent.com",
            "INVOICE_COLLECTOR_WEB_CLIENT_SECRET": "made-up-value",
            **LINKS_ALONE,
        },
        serve=lambda app, host, port: None,
    )

    assert exit_code == 2
    assert ALONE in capsys.readouterr().err


def test_the_runner_refuses_to_start_with_the_setting_alone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = runner_serve.main(
        ["--ledger", str(tmp_path / "ledger.sqlite")],
        environment={"INVOICE_COLLECTOR_RUNNER_SECRET": "a-secret-only-for-tests", **LINKS_ALONE},
        serve=lambda app, host, port: None,
    )

    assert exit_code == 2
    assert ALONE in capsys.readouterr().err


def test_the_collect_command_refuses_to_run_with_the_setting_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("INVOICE_COLLECTOR_SAMPLE_PORTAL_URL", raising=False)
    monkeypatch.setenv("INVOICE_COLLECTOR_SAMPLE_PORTAL_LINKS", WRITTEN)

    exit_code = collect_main(
        ["collect", "2026-08", "--account", ENGINEERING, "--out", str(tmp_path)]
    )

    assert exit_code == 2
    assert ALONE in capsys.readouterr().err


# Through the real browser, against a local server standing for the sample portal


@pytest.fixture
def sites() -> Iterator[tuple[Site, Site]]:
    """The sample portal, and a server at the address the links were written for, which
    nothing should reach."""
    portal, written = Site(), Site()
    servers = [portal.start(), written.start()]
    portal.pages = {"/in_1PqX7fK2.html": INVOICE, "/sign-in.html": SIGN_IN}
    portal.redirects = {"/to-the-written-address": f"{localhost(written)}/secret"}
    yield portal, written
    for server in servers:
        server.shutdown()
        server.server_close()


def localhost(site: Site) -> str:
    """The site's address by the name localhost, as the sample generator writes links."""
    return site.url.replace("127.0.0.1", "localhost")


@pytest.mark.browser
def test_the_real_browser_opens_a_link_written_for_another_address_at_the_sample_portal(
    sites: tuple[Site, Site],
) -> None:
    portal, written = sites
    written_with = localhost(written)
    policy = DestinationPolicy.from_environment(
        {
            "INVOICE_COLLECTOR_SAMPLE_PORTAL_URL": portal.url,
            "INVOICE_COLLECTOR_SAMPLE_PORTAL_LINKS": written_with,
        }
    )
    other_port = f"http://localhost:{portal.url.rsplit(':', 1)[1]}/in_1PqX7fK2.html"
    with_a_user = written_with.replace("http://", "http://finance:secret@") + "/in_1PqX7fK2.html"
    refusals: list[str] = []

    with HeadlessBrowser(policy, settle_ms=500) as browser:
        fetcher = OpenedWhereTheyLead(browser, policy.address_to_open)
        pdf = fetcher.fetch(f"{written_with}/in_1PqX7fK2.html")
        gated = fetcher.fetch(f"{written_with}/sign-in.html")
        for refused in (f"{written_with}/to-the-written-address", other_port, with_a_user):
            with pytest.raises(PortalFetchFailed) as failure:
                fetcher.fetch(refused)
            refusals.append(str(failure.value))

    assert isinstance(pdf, bytes) and pdf.startswith(b"%PDF-")
    assert gated == LoginGated()
    # A redirect from the portal back to the address the links were written for is checked
    # as any redirect is, and refused: that address is never opened itself.
    assert "http links are not followed" in refusals[0]
    assert f"(the link in the email is {written_with}/to-the-written-address)" in refusals[0]
    # Another port of that host, and a link carrying a user name, are refused as written.
    assert refusals[1] == f"could not open {other_port}: http links are not followed"
    assert refusals[2] == f"could not open {with_a_user}: http links are not followed"
    assert portal.requested == ["/in_1PqX7fK2.html", "/sign-in.html", "/to-the-written-address"]
    assert written.requested == []
