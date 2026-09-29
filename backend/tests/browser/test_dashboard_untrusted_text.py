"""Text that came in an email is shown as text, and never runs as part of the page.

Anyone can send an email to a source account (ADR 0012), so a sender, a subject or the
vendor name a billing document gives may hold HTML. A run collects one such document and
holds another, and the screens that show them must show the HTML as the characters it is.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from playwright.sync_api import Dialog, Page, expect

from invoice_collector.archive import LocalArchive
from invoice_collector.classifier import FakeClassifier
from invoice_collector.domain import Attachment, Email, ExpectedVendor, Extraction
from invoice_collector.extractor import FakeExtractor
from invoice_collector.ledger import Ledger
from invoice_collector.mail_source import InMemoryMailSource
from invoice_collector.run import Pipeline, collect
from invoice_collector.vendor_matcher import RulesFirstVendorMatcher

from .harness import AUGUST, OpenDashboard, SampleBrowser, pdf_of, rates, sign_in

pytestmark = [pytest.mark.dashboard, pytest.mark.browser]

HOSTILE = "<img src=x onerror=alert(1)>"
ENGINEERING = "engineering@nyayalabs.example"
COLLECTED_VENDOR = f"Acme {HOSTILE}"
COLLECTED_SUBJECT = f"Your invoice {HOSTILE}"
HELD_VENDOR = f"Held {HOSTILE}"
HELD_SUBJECT = f"Second invoice {HOSTILE}"
SENDER = f"{HOSTILE} <billing@hostile.example>"


def email(message_id: str, subject: str, day: int, pdf: bytes) -> Email:
    return Email(
        source_account=ENGINEERING,
        message_id=message_id,
        sender=SENDER,
        subject=subject,
        received_at=datetime(2026, 8, day, 9, 0, tzinfo=UTC),
        attachments=(Attachment("invoice.pdf", "application/pdf", pdf),),
    )


@pytest.fixture
def hostile(tmp_path: Path) -> Path:
    """A folder a run wrote from two emails whose sender, subject and vendor hold HTML.

    One billing document is collected. The other is held, since its total is ten times
    what its vendor usually bills.
    """
    out = tmp_path / "out"
    out.mkdir()
    collected, held = pdf_of("hostile collected"), pdf_of("hostile held")
    ledger = Ledger(out / "ledger.sqlite")
    try:
        ledger.save_expected_vendor(
            ExpectedVendor(HELD_VENDOR, ENGINEERING, "monthly", None, Decimal("10.00"), "USD")
        )
        collect(
            AUGUST,
            sources=[
                InMemoryMailSource(
                    ENGINEERING,
                    [
                        email("m-hostile-collected", COLLECTED_SUBJECT, 4, collected),
                        email("m-hostile-held", HELD_SUBJECT, 6, held),
                    ],
                )
            ],
            pipeline=Pipeline(
                classifier=FakeClassifier(),
                extractor=FakeExtractor.for_documents(
                    {
                        collected: Extraction(
                            "invoice", COLLECTED_VENDOR, date(2026, 8, 4), Decimal("120.00"), "USD"
                        ),
                        held: Extraction(
                            "invoice", HELD_VENDOR, date(2026, 8, 6), Decimal("100.00"), "USD"
                        ),
                    }
                ),
                renderer=SampleBrowser(),
                portal_fetcher=SampleBrowser(),
                exchange_rates=rates(),
                archive=LocalArchive(out / "archive"),
                ledger=ledger,
                vendor_matcher=RulesFirstVendorMatcher(),
            ),
            summary_writers=[],
        )
    finally:
        ledger.close()
    return out


@pytest.fixture
def dialogs(page: Page) -> list[str]:
    """What any dialog opened on the page said. A script from an email would open one."""
    opened: list[str] = []

    def dismiss(dialog: Dialog) -> None:
        opened.append(dialog.message)
        dialog.dismiss()

    page.on("dialog", dismiss)
    return opened


def expect_no_markup_ran(page: Page, dialogs: list[str]) -> None:
    # Once nothing more is loading, an image the HTML had made would have failed and run.
    page.wait_for_load_state("networkidle")
    expect(page.get_by_role("img")).to_have_count(0)
    assert dialogs == []


def test_email_text_on_the_summary_is_shown_as_text(
    page: Page, dialogs: list[str], open_dashboard: OpenDashboard, hostile: Path
) -> None:
    dashboard = open_dashboard(hostile)
    sign_in(page, dashboard)

    documents = page.get_by_role("region", name="Billing documents", exact=True)
    expect(documents.get_by_role("cell", name=COLLECTED_VENDOR, exact=True)).to_be_visible()
    needing_review = page.get_by_role("region", name="Emails needing review")
    expect(needing_review.get_by_role("cell", name=HELD_SUBJECT, exact=True)).to_be_visible()
    expect_no_markup_ran(page, dialogs)


def test_email_text_on_the_review_screen_is_shown_as_text(
    page: Page, dialogs: list[str], open_dashboard: OpenDashboard, hostile: Path
) -> None:
    dashboard = open_dashboard(hostile)
    sign_in(page, dashboard)

    page.get_by_role("navigation", name="Screens").get_by_role("link", name="Review").click()
    queue = page.get_by_role("list", name="Emails needing review")
    expect(queue.get_by_role("button")).to_contain_text(HELD_VENDOR)
    expect(page.get_by_role("textbox", name="Vendor")).to_have_value(HELD_VENDOR)
    fields = page.get_by_role("complementary", name="Extracted fields")
    expect(fields).to_contain_text(SENDER)
    expect(fields).to_contain_text(HELD_SUBJECT)
    expect_no_markup_ran(page, dialogs)

    # And in the history of the document, which names the email it came in.
    page.get_by_role("link", name="How this document was found, read and checked").click()
    history = page.get_by_role("list", name="History")
    expect(history).to_contain_text(HELD_SUBJECT)
    expect(history).to_contain_text(SENDER)
    expect_no_markup_ran(page, dialogs)
