"""Fixtures for driving the real dashboard in a headless browser.

The front end must be built first (npm run build in frontend/); without it these tests are
skipped, with a message saying so. Each test gets its own copy of a ledger that the collect
command filled from the sample mail, and its own dashboard serving it with the built pages.
"""

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Page, sync_playwright

from .harness import BUILT_FRONTEND, OpenDashboard, collect_samples

LIVE_KEYS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "JEV_API_KEY")


@pytest.fixture(scope="session")
def built_frontend() -> Path:
    if not (BUILT_FRONTEND / "index.html").is_file():
        pytest.skip(
            f"the front end is not built, so the browser tests cannot run: there is no "
            f"{BUILT_FRONTEND / 'index.html'}. Run npm ci and npm run build in frontend/."
        )
    return BUILT_FRONTEND


@pytest.fixture(scope="session")
def collected_august(built_frontend: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A folder the collect command wrote for August 2026 from the sample mail."""
    out = tmp_path_factory.mktemp("collected") / "out"
    with pytest.MonkeyPatch.context() as environment:
        for name in LIVE_KEYS:
            environment.delenv(name, raising=False)
        collect_samples(out)
    return out


@pytest.fixture
def august(collected_august: Path, tmp_path: Path) -> Path:
    """This test's own copy of the collected folder, so what it changes stays its own."""
    out = tmp_path / "out"
    shutil.copytree(collected_august, out)
    return out


@pytest.fixture
def open_dashboard(built_frontend: Path) -> Iterator[OpenDashboard]:
    opened = OpenDashboard(built_frontend)
    yield opened
    opened.stop()


@pytest.fixture(scope="module")
def chromium(built_frontend: Path) -> Iterator[Browser]:
    # Closed when its module ends, so it never shares the thread with another test's browser.
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        yield browser
        browser.close()


@pytest.fixture
def page(chromium: Browser) -> Iterator[Page]:
    """A fresh browser page, failing the test if a script on it throws."""
    context = chromium.new_context()
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    yield page
    context.close()
    assert errors == [], f"a script on the page threw: {errors}"
