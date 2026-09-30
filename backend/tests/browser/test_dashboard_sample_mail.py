"""Putting the sample mail into a test mailbox from the Source accounts screen, in a browser.

A reviewer with only Docker has no command line to seed her mailboxes with. Google is a
stand-in that sends her straight back to the dashboard having granted leave to insert, and
Gmail is a fake that remembers what each mailbox holds.
"""

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

import pytest
from google.oauth2.credentials import Credentials
from playwright.sync_api import Page, expect

from invoice_collector import google_auth
from invoice_collector.api.sample_mail import FakeSampleMailInserter, SampleMail
from invoice_collector.api.source_account_connector import FakeSourceAccountConnector
from invoice_collector.google_auth import GMAIL_READONLY, SEEDING
from invoice_collector.ledger import Ledger

from .harness import SAMPLES, SOURCE_ACCOUNTS, OpenDashboard, sign_in

pytestmark = [pytest.mark.dashboard, pytest.mark.browser]

MAILBOX = SOURCE_ACCOUNTS[0]
SAMPLE = "ops@nyayalabs.example"
PORTAL = "http://portal:8765"


class GoogleGrantsLeave(FakeSourceAccountConnector):
    """Sends the person straight back to the dashboard, as having granted what was asked."""

    def __init__(self, dashboard_url: str) -> None:
        super().__init__({f"code-{address}": address for address in SOURCE_ACCOUNTS})
        self._callback = f"{dashboard_url}/accounts/callback"

    def authorization_url(self, state: str, address: str, scopes: Sequence[str]) -> str:
        return f"{self._callback}?{urlencode({'state': state, 'code': f'code-{address}'})}"


def store_reading_sign_in(token_dir: Path, address: str) -> None:
    google_auth.store_sign_in(
        address,
        Credentials(  # pyright: ignore[reportUnknownVariableType]
            token="made-up-access",
            refresh_token="made-up-refresh",
            token_uri="http://127.0.0.1:9/never-called",
            client_id="made-up.apps.googleusercontent.com",
            client_secret="made-up",
            scopes=[GMAIL_READONLY],
            expiry=datetime(2999, 1, 1),
        ),
        token_dir,
    )


def test_an_administrator_fills_a_test_mailbox_with_the_sample_mail_and_its_vendors(
    page: Page, open_dashboard: OpenDashboard, august: Path
) -> None:
    store_reading_sign_in(august / "tokens", MAILBOX)
    inserter = FakeSampleMailInserter()
    dashboard = open_dashboard(
        august,
        connector_for=GoogleGrantsLeave,
        sample_mail=SampleMail(SAMPLES, PORTAL),
        sample_mail_inserter=inserter,
    )
    sign_in(page, dashboard)
    page.get_by_role("navigation", name="Screens").get_by_role(
        "link", name="Source accounts"
    ).click()
    card = page.get_by_role("article", name=MAILBOX)

    card.get_by_role("button", name="Fill with sample mail").click()
    form = page.get_by_role("form", name=f"Fill {MAILBOX} with sample mail")
    expect(form.get_by_role("note")).to_contain_text("It is meant for test mailboxes only")
    form.get_by_label("Sample mailbox", exact=True).select_option(SAMPLE)
    form.get_by_role("checkbox", name="Also add the 7 vendors").check()
    form.get_by_role("button", name=f"Put sample mail into {MAILBOX}").click()

    expect(page.get_by_text(f"Put 31 sample emails from {SAMPLE} into {MAILBOX}")).to_be_visible()
    assert len(inserter.held[MAILBOX]) == 31
    assert google_auth.sign_in_file(MAILBOX, august / "tokens", SEEDING).is_file()
    # Every vendor of the sample mailbox is already on this list, billed to the sample
    # mailbox, so each is left as it is and said to be, and none is moved without asking.
    expect(page.get_by_text("Already on the list and left as they are")).to_contain_text("Slack")
    ledger = Ledger(august / "ledger.sqlite")
    try:
        slack = next(v for v in ledger.expected_vendors() if v.vendor == "Slack")
    finally:
        ledger.close()
    assert slack.source_account == SAMPLE
    expect(page.get_by_role("region", name="Changes")).to_contain_text(
        f"Put sample mail into {MAILBOX}"
    )


def test_a_mailbox_whose_sign_in_does_not_work_is_not_offered_the_sample_mail(
    page: Page, open_dashboard: OpenDashboard, august: Path
) -> None:
    dashboard = open_dashboard(
        august,
        connector_for=GoogleGrantsLeave,
        sample_mail=SampleMail(SAMPLES, PORTAL),
        sample_mail_inserter=FakeSampleMailInserter(),
    )
    sign_in(page, dashboard)
    page.get_by_role("navigation", name="Screens").get_by_role(
        "link", name="Source accounts"
    ).click()

    card = page.get_by_role("article", name=MAILBOX)
    expect(card.get_by_text("Not signed in")).to_be_visible()
    expect(card.get_by_role("button", name="Fill with sample mail")).to_have_count(0)
