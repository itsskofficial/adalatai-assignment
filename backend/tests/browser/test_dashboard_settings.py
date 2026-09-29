"""The Settings screen, driven in a real browser as an administrator drives it.

The dashboard's clock is fixed at 10:00 UTC on 1 September 2026, so the next scheduled run
has one right answer. A stand-in for the runner records each time it is told the schedule
changed; tests/test_api_settings.py tells the real runner.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from invoice_collector.collection_settings import SettingsStore

from .harness import OpenDashboard, sign_in

pytestmark = [pytest.mark.dashboard, pytest.mark.browser]

NOW = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)


class Runner:
    def __init__(self) -> None:
        self.told = 0

    def schedule_changed(self) -> None:
        self.told += 1


def test_changing_the_day_changes_when_the_next_run_is_due(
    page: Page, open_dashboard: OpenDashboard, august: Path
) -> None:
    runner = Runner()
    dashboard = open_dashboard(august, schedule_keeper=runner, now=lambda: NOW)
    sign_in(page, dashboard)
    page.get_by_role("navigation", name="Screens").get_by_role("link", name="Settings").click()
    form = page.get_by_role("form", name="Settings")
    next_run = page.get_by_label("Next scheduled run")
    save = form.get_by_role("button", name="Save settings")
    expect(next_run).to_have_text("The schedule is off. Nothing runs by itself.")

    form.get_by_label("Collect on a schedule").check()
    form.get_by_label("Day of the month").fill("5")
    save.click()

    expect(page.get_by_text("The runner has the new schedule.")).to_be_visible()
    expect(next_run).to_have_text(
        "Next run: 5 Sep 2026 at 06:00 (Asia/Kolkata), collecting August 2026."
    )

    # The 1st has already gone by this month, so the next run is in October, and collects
    # September, the month that will then have just ended.
    form.get_by_label("Day of the month").fill("1")
    save.click()

    expect(next_run).to_have_text(
        "Next run: 1 Oct 2026 at 06:00 (Asia/Kolkata), collecting September 2026."
    )
    assert runner.told == 2

    page.reload()
    expect(form.get_by_label("Day of the month")).to_have_value("1")
    expect(form.get_by_label("Collect on a schedule")).to_be_checked()
    changes = page.get_by_role("region", name="Changes to the settings")
    expect(changes).to_contain_text("changed the day of the month from 5 to 1")
    expect(changes).to_contain_text("changed the schedule from off to on")


def test_changing_the_drive_folder_is_kept_for_later_runs(
    page: Page, open_dashboard: OpenDashboard, august: Path
) -> None:
    runner = Runner()
    dashboard = open_dashboard(august, schedule_keeper=runner, now=lambda: NOW)
    sign_in(page, dashboard)
    page.get_by_role("navigation", name="Screens").get_by_role("link", name="Settings").click()
    form = page.get_by_role("form", name="Settings")
    folder = form.get_by_label("Folder in the owner account's Drive")
    expect(folder).to_have_value("Invoice Collection")
    expect(form).to_contain_text("nothing already filed is moved")

    folder.fill("Finance invoices")
    form.get_by_role("button", name="Save settings").click()

    expect(page.get_by_text("The settings are saved.")).to_be_visible()
    expect(page.get_by_role("region", name="Changes to the settings")).to_contain_text(
        "changed the Drive folder from Invoice Collection to Finance invoices"
    )
    assert SettingsStore(august / "ledger.sqlite").read().drive_folder == "Finance invoices"
    # Only the schedule is the runner's to hear of.
    assert runner.told == 0
