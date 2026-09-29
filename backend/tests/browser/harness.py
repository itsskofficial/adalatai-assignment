"""What the browser tests share: a ledger filled by a real run, and the real dashboard serving it.

Only the outside world is faked. Mail is the sample mail in backend/samples; billing
documents are read from the answer key generated with it, handed to the run as the model
would be, since the collect command has no option to read it; rates to rupees are fixed; email
bodies are "rendered" without a browser; and a portal link leads nowhere, except the sample
sign-in page, which is login-gated as a real one is. Everything else, from the collect
command's run to the dashboard's API and the front end, is the code that ships.
"""

import argparse
import io
import json
import socket
import threading
import time
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode

import anthropic
import uvicorn
from fastapi import FastAPI
from playwright.sync_api import Page, expect
from pypdf import PdfWriter

from invoice_collector.api.app import create_app
from invoice_collector.api.collection_runner import CollectionRunner
from invoice_collector.api.collection_settings_routes import ScheduleKeeper
from invoice_collector.api.identity import IdentityNotVerified
from invoice_collector.api.settings import Settings as DashboardSettings
from invoice_collector.cli import add_collection_options, run_collection
from invoice_collector.destinations import DestinationPolicy
from invoice_collector.domain import CollectionMonth, StartedBy
from invoice_collector.evals.answer_key import answer_key
from invoice_collector.exchange_rates import FakeExchangeRates
from invoice_collector.extractor import Extractor
from invoice_collector.ledger import Ledger
from invoice_collector.mail_source import MailSource
from invoice_collector.portal import LoginGated, PortalFetchFailed
from invoice_collector.run import Pipeline, RunResult, Settings, collect
from invoice_collector.source_account_registry import SourceAccountRegistry
from invoice_collector.summary import SummaryWriter
from invoice_collector.vendor_matcher import RulesFirstVendorMatcher

BACKEND = Path(__file__).resolve().parents[2]
SAMPLES = BACKEND / "samples"
BUILT_FRONTEND = BACKEND.parent / "frontend" / "dist"
RECORDED = BACKEND / "tests" / "recorded"

AUGUST = CollectionMonth(2026, 8)
FINANCE = "finance@nyayalabs.example"
SOURCE_ACCOUNTS = (
    "engineering@nyayalabs.example",
    "finance@nyayalabs.example",
    "ops@nyayalabs.example",
)
# A vendor that bills in August and is left off the expected vendor list, to be suggested.
SUGGESTED = "Atlassian"
# The code the stand-in for Google hands back for the person signing in.
SIGN_IN_CODE = "code-finance"

# Options of the collect command that keep a run away from every live service. Documents
# are read by the answer key, which run_over_samples hands in.
OFFLINE = (
    "--classifier",
    "rules",
    "--vendor-matcher",
    "rules",
    "--no-exchange-rates",
    "--no-digest",
)


def pdf_of(title: str) -> bytes:
    """A real one-page PDF with no text, distinct for each title."""
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_metadata({"/Title": title})
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def rates() -> FakeExchangeRates:
    return FakeExchangeRates(
        {"USD": Decimal("95.34"), "EUR": Decimal("103.20"), "GBP": Decimal("121.05")}
    )


class SampleBrowser:
    """Renders email bodies and fetches portal links, with no browser and no network.

    The sample portal's sign-in page is login-gated, so its email waits for an upload.
    """

    def render_html(self, html: str) -> bytes:
        return pdf_of(f"rendered {len(html)} {hash(html)}")

    def fetch(self, url: str) -> bytes | LoginGated:
        if url.endswith("/sign-in.html"):
            return LoginGated()
        raise PortalFetchFailed(f"could not open {url}")


@contextmanager
def sample_browser(policy: DestinationPolicy) -> Generator[SampleBrowser]:
    yield SampleBrowser()


def collect_with_rates(
    month: CollectionMonth,
    *,
    sources: Sequence[MailSource],
    pipeline: Pipeline,
    summary_writers: Sequence[SummaryWriter],
    settings: Settings | None = None,
    started_by: StartedBy = "command_line",
) -> RunResult:
    """The run itself, valuing documents in rupees at fixed rates rather than looking them up."""
    return collect(
        month,
        sources=sources,
        pipeline=replace(pipeline, exchange_rates=rates()),
        summary_writers=summary_writers,
        settings=settings,
        started_by=started_by,
    )


def run_over_samples(
    month: CollectionMonth, args: argparse.Namespace, *, started_by: StartedBy
) -> int:
    """The collect command's run, reading the sample mail instead of the source accounts.

    Given to the dashboard's runner, so the Runs screen starts the run that ships.
    """
    args.samples = SAMPLES
    args.account = []
    args.connected_accounts = False
    return run_collection(
        month,
        args,
        collector=collect_with_rates,
        browser=sample_browser,
        started_by=started_by,
        extractor=answer_key(SAMPLES),
    )


def collect_samples(out: Path, month: CollectionMonth = AUGUST) -> None:
    """Runs the collect command over the sample mail into the folder, as an engineer would.

    The expected vendor list it starts from leaves out SUGGESTED, which bills in August, so
    the run suggests it. The sample source accounts are then connected, as a person connects
    them on the Source accounts screen, so a run can be started from the Runs screen.
    """
    out.mkdir(parents=True)
    listed = json.loads((SAMPLES / "expected_vendors.json").read_text(encoding="utf-8"))
    expected = out.parent / "expected_vendors.json"
    expected.write_text(
        json.dumps([each for each in listed if each["vendor"] != SUGGESTED]), encoding="utf-8"
    )
    parser = argparse.ArgumentParser()
    parser.add_argument("month", type=CollectionMonth.parse)
    add_collection_options(parser)
    args = parser.parse_args(
        [
            str(month),
            "--out",
            str(out),
            "--token-dir",
            str(out / "tokens"),
            "--expected-vendors",
            str(expected),
            *OFFLINE,
        ]
    )
    outcome: list[int | BaseException] = []

    def run() -> None:
        try:
            outcome.append(run_over_samples(month, args, started_by="command_line"))
        except BaseException as error:
            outcome.append(error)

    # On a thread of its own, as a run started from the dashboard is, so it never shares a
    # thread with the browser the tests drive.
    thread = threading.Thread(target=run, name="collect-samples")
    thread.start()
    thread.join()
    if outcome != [0]:
        raise RuntimeError(f"the sample collection did not complete: {outcome}")
    registry = SourceAccountRegistry(out / "ledger.sqlite")
    for account in SOURCE_ACCOUNTS:
        registry.signed_in(
            account, "connected", FINANCE, datetime.now(UTC), signed_in_at=None, fingerprint=None
        )


class GoogleSaysYes:
    """Stands in for Google: sends the person straight back to the dashboard, signed in.

    The dashboard's own sign-in route and callback run as they do in production; only
    Google's page is skipped. It is passed to create_app by the tests alone.
    """

    def __init__(self, dashboard_url: str, emails: dict[str, str]) -> None:
        self._callback = f"{dashboard_url}/auth/callback"
        self._emails = emails

    def authorization_url(self, state: str) -> str:
        return f"{self._callback}?{urlencode({'state': state, 'code': SIGN_IN_CODE})}"

    def verified_email(self, code: str) -> str:
        if code not in self._emails:
            raise IdentityNotVerified("unknown authorization code")
        return self._emails[code]


class ServedDashboard:
    """The dashboard's app served on a port of this machine, on a thread of its own."""

    def __init__(self) -> None:
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.bind(("127.0.0.1", 0))
        # Listening before the server starts, so a request made at once waits to be answered.
        self._socket.listen()
        port = self._socket.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    def serve(self, app: FastAPI) -> None:
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off"))
        thread = threading.Thread(
            target=server.run, kwargs={"sockets": [self._socket]}, name="dashboard", daemon=True
        )
        thread.start()
        self._server, self._thread = server, thread
        deadline = time.monotonic() + 10
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("the dashboard did not start")
            time.sleep(0.01)

    def stop(self) -> None:
        if self._server is not None and self._thread is not None:
            self._server.should_exit = True
            self._thread.join(10)
        self._socket.close()


class OpenDashboard:
    """Starts the dashboard over a folder a collection wrote, as the dashboard command does.

    With the built front end, the fakes above for the outside world, and the collect
    command's run behind the Runs screen. A Claude client and an extractor for uploads, a
    stand-in for the runner that keeps the schedule, and a fixed clock are given by the
    tests that need them.
    """

    def __init__(self, frontend: Path) -> None:
        self._frontend = frontend
        self._served: list[ServedDashboard] = []

    def __call__(
        self,
        out: Path,
        *,
        claude: anthropic.Anthropic | None = None,
        extractor: Extractor | None = None,
        schedule_keeper: ScheduleKeeper | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> ServedDashboard:
        dashboard = ServedDashboard()
        self._served.append(dashboard)
        ledger_path = out / "ledger.sqlite"
        settings = DashboardSettings(
            session_secret="a-secret-only-for-browser-tests",
            allowlist=frozenset({FINANCE}),
            ledger_path=ledger_path,
            token_dir=out / "tokens",
            redirect_uri=f"{dashboard.url}/auth/callback",
            accounts_redirect_uri=f"{dashboard.url}/accounts/callback",
            frontend_origin=dashboard.url,
            frontend_dir=self._frontend,
        )
        app = create_app(
            settings,
            lambda: Ledger(ledger_path),
            GoogleSaysYes(dashboard.url, {SIGN_IN_CODE: FINANCE}),
            claude=claude,
            exchange_rates=rates(),
            extractor=extractor,
            vendor_matcher=RulesFirstVendorMatcher(),
            runner=CollectionRunner(
                ledger_path, out / "tokens", options=OFFLINE, run=run_over_samples
            ),
            schedule_keeper=schedule_keeper,
            now=now or (lambda: datetime.now(UTC)),
        )
        dashboard.serve(app)
        return dashboard

    def stop(self) -> None:
        for dashboard in self._served:
            dashboard.stop()


def sign_in(page: Page, dashboard: ServedDashboard, path: str = "/") -> None:
    """Opens the dashboard and signs in with Google as a person does, landing on the path."""
    page.goto(f"{dashboard.url}{path}")
    page.get_by_role("button", name="Sign in with Google").click()
    expect(page.get_by_role("button", name="Sign out")).to_be_visible()
