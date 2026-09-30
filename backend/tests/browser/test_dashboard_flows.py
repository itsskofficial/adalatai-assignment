"""The dashboard's key flows, driven in a real browser the way a person in finance drives them.

Each test signs in through the dashboard's own sign-in, moves between screens by the links
in the header, finds what it needs by its role and name, and waits for what the person
would see. The ledger behind the screens is what the collect command wrote from the sample
mail for August 2026: eight billing documents, the Datadog invoice held because its total
is 39% above the usual, the Google Workspace invoice behind a portal sign-in, and
Atlassian billing without being on the expected vendor list.
"""

import json
import re
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import anthropic
import pytest
from playwright.sync_api import Locator, Page, expect

from invoice_collector.domain import Extraction
from invoice_collector.extractor import FakeExtractor

from .harness import FINANCE, RECORDED, SUGGESTED, OpenDashboard, pdf_of, sign_in

pytestmark = [pytest.mark.dashboard, pytest.mark.browser]

HELD_REASON = "The total is 39% above the usual 663.00 USD for Datadog"
GOOGLE_WORKSPACE_EMAIL = "Google Workspace: your invoice is available for nyayalabs.example"

# A Claude client replaying one recorded answer from a local server: see tests/conftest.py.
ReplayClient = Callable[[int, dict[str, Any]], anthropic.Anthropic]


def go_to(page: Page, screen: str) -> None:
    page.get_by_role("navigation", name="Screens").get_by_role("link", name=screen).click()


def section(page: Page, name: str) -> Locator:
    return page.get_by_role("region", name=name, exact=True)


def summary_row(page: Page, file_name: str) -> Locator:
    return section(page, "Billing documents").get_by_role("row").filter(has_text=file_name)


def test_a_held_document_is_approved_and_joins_the_summary(
    page: Page, open_dashboard: OpenDashboard, august: Path
) -> None:
    dashboard = open_dashboard(august)
    sign_in(page, dashboard)
    expect(section(page, "Gaps").get_by_role("row").filter(has_text="Datadog")).to_contain_text(
        "held for review"
    )

    go_to(page, "Review")
    queue = page.get_by_role("list", name="Emails needing review")
    expect(queue.get_by_role("button")).to_have_count(1)
    expect(queue.get_by_role("button", name=re.compile("^Datadog"))).to_be_visible()
    expect(page.get_by_role("list", name="Reasons for review")).to_have_text(HELD_REASON)
    page.get_by_role("button", name="Approve").click()

    expect(page.get_by_text("Nothing needs review for August 2026.")).to_be_visible()
    go_to(page, "Summary")
    row = summary_row(page, "2026-08_Datadog_921.70-USD.pdf")
    expect(row).to_contain_text("921.70")
    # Valued in rupees at the rate on its invoice date, as a run values one.
    expect(row).to_contain_text("87,874.88")
    expect(section(page, "Gaps").get_by_role("row").filter(has_text="Datadog")).to_have_count(0)


def test_a_corrected_field_is_what_is_filed_and_summarised(
    page: Page, open_dashboard: OpenDashboard, august: Path
) -> None:
    dashboard = open_dashboard(august)
    sign_in(page, dashboard)

    go_to(page, "Review")
    total = page.get_by_role("textbox", name="Total", exact=True)
    expect(total).to_have_value("921.70")
    total.fill("663.00")
    expect(page.get_by_text("2026-08_Datadog_663.00-USD.pdf", exact=True)).to_be_visible()
    page.get_by_role("button", name="Approve").click()

    expect(page.get_by_text("Nothing needs review for August 2026.")).to_be_visible()
    go_to(page, "Summary")
    row = summary_row(page, "2026-08_Datadog_663.00-USD.pdf")
    expect(row).to_contain_text("663.00")
    expect(row).to_contain_text("63,210.42")
    expect(summary_row(page, "921.70")).to_have_count(0)

    # The correction is part of the document's history, with who made it.
    row.get_by_role("link", name="History of 2026-08_Datadog_663.00-USD.pdf").click()
    corrected = (
        page.get_by_role("list", name="History")
        .get_by_role("listitem")
        .filter(has=page.get_by_role("heading", name="Total corrected"))
    )
    expect(corrected).to_contain_text("921.70 → 663.00")
    expect(corrected).to_contain_text(f"By {FINANCE}")


def test_a_suggested_vendor_is_accepted_onto_the_expected_list(
    page: Page, open_dashboard: OpenDashboard, august: Path
) -> None:
    dashboard = open_dashboard(august)
    sign_in(page, dashboard)

    go_to(page, "Vendors")
    suggestion = section(page, "Suggested vendors").get_by_role("article", name=SUGGESTED)
    expect(suggestion).to_contain_text("Billed in August 2026")
    expected = page.get_by_role("table", name="Expected vendors")
    expect(expected.get_by_role("rowheader", name=SUGGESTED)).to_have_count(0)
    suggestion.get_by_role("button", name="Accept", exact=True).click()

    expect(expected.get_by_role("rowheader", name=SUGGESTED, exact=True)).to_be_visible()
    expect(section(page, "Suggested vendors")).to_have_count(0)
    atlassian = expected.get_by_role("row").filter(
        has=page.get_by_role("rowheader", name=SUGGESTED, exact=True)
    )
    expect(atlassian).to_contain_text("Monthly")


def test_a_pdf_downloaded_from_a_portal_is_uploaded_and_filed(
    page: Page, open_dashboard: OpenDashboard, august: Path
) -> None:
    downloaded = pdf_of("Google Workspace invoice for August 2026")
    read = Extraction("invoice", "Google Workspace", date(2026, 8, 2), Decimal("45878.40"), "INR")
    dashboard = open_dashboard(august, extractor=FakeExtractor.for_documents({downloaded: read}))
    sign_in(page, dashboard)

    go_to(page, "Review")
    manual = page.get_by_role("region", name="Manual download needed")
    expect(manual).to_contain_text(GOOGLE_WORKSPACE_EMAIL)
    manual.get_by_label(f"PDF for {GOOGLE_WORKSPACE_EMAIL}").set_input_files(
        files=[{"name": "invoice.pdf", "mimeType": "application/pdf", "buffer": downloaded}]
    )
    manual.get_by_role("button", name="Upload").click()

    expect(page.get_by_role("status")).to_have_text(
        "Filed 2026-08_GoogleWorkspace_45878.40-INR.pdf and added to the summary."
    )
    expect(manual).to_have_count(0)
    go_to(page, "Summary")
    expect(summary_row(page, "2026-08_GoogleWorkspace_45878.40-INR.pdf")).to_contain_text(
        "45,878.40"
    )


def test_a_question_is_answered_with_the_documents_behind_it(
    page: Page,
    open_dashboard: OpenDashboard,
    august: Path,
    replay_client: ReplayClient,
    received_requests: list[dict[str, Any]],
) -> None:
    # Claude's recorded choice: total spend on AWS from June to August 2026.
    recorded = json.loads((RECORDED / "claude_question_tool_use.json").read_text("utf-8"))
    dashboard = open_dashboard(august, claude=replay_client(200, recorded))
    sign_in(page, dashboard)

    go_to(page, "Questions")
    question = "How much did we spend on AWS last quarter?"
    page.get_by_role("textbox", name="Question").fill(question)
    page.get_by_role("button", name="Ask").click()

    asked = page.get_by_role("article", name=question)
    expect(asked).to_contain_text("Total spend on AWS")
    expect(asked).to_contain_text("2,57,168.21")
    behind = asked.get_by_role("table", name="Billing documents behind this answer")
    expect(behind.get_by_role("row").filter(has_text="2026-08_AWS_2697.38-USD.pdf")).to_be_visible()
    # The question went to the stand-in for Claude, and to nothing else.
    assert len(received_requests) == 1
    assert question in json.dumps(received_requests[0])


def test_a_month_is_run_again_from_the_runs_screen_and_seen_to_finish(
    page: Page, open_dashboard: OpenDashboard, august: Path
) -> None:
    dashboard = open_dashboard(august)
    sign_in(page, dashboard)

    go_to(page, "Runs")
    runs = page.get_by_role("region", name="Runs of the month")
    expect(runs.get_by_role("article")).to_have_count(1)
    expect(runs.get_by_role("article")).to_contain_text("From the command line")
    page.get_by_role("button", name="Run August 2026 again").click()

    started = page.get_by_role("status").filter(has_text="Started a run of August 2026")
    expect(started).to_be_visible()
    ours = runs.get_by_role("article").filter(has_text=f"From the dashboard by {FINANCE}")
    # The screen follows the run by itself until it ends; the run takes a few seconds.
    expect(ours).to_contain_text("Finished", timeout=60_000)
    expect(runs.get_by_role("article")).to_have_count(2)
    expect(ours).not_to_contain_text("Not yet known")
    expect(page.get_by_text("A run of August 2026 is going on")).to_have_count(0)


def test_the_history_of_a_billing_document_opens_from_the_summary(
    page: Page, open_dashboard: OpenDashboard, august: Path
) -> None:
    dashboard = open_dashboard(august)
    sign_in(page, dashboard)

    summary_row(page, "2026-08_Slack_652.50-USD.pdf").get_by_role(
        "link", name="History of 2026-08_Slack_652.50-USD.pdf"
    ).click()

    expect(page.get_by_role("heading", level=1)).to_have_text(
        "History of Slack invoice, 652.50 USD"
    )
    about = page.get_by_role("region", name="About this document")
    expect(about).to_contain_text("Collected")
    expect(about).to_contain_text("2026-08_Slack_652.50-USD.pdf")
    history = page.get_by_role("list", name="History")
    expect(
        history.get_by_role("heading", name="Email received in ops@nyayalabs.example")
    ).to_be_visible()
    expect(history.get_by_role("heading", name="Classified as an invoice").first).to_be_visible()

    page.get_by_role("link", name="Back to the summary").click()
    expect(page.get_by_role("heading", name="Summary for August 2026")).to_be_visible()
