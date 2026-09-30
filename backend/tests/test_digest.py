"""The digest sent to Slack after a run, checked without ever calling Slack."""

import hashlib
import json
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from email.message import EmailMessage
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from invoice_collector.cli import main
from invoice_collector.digest import (
    Digest,
    DigestNotSent,
    DigestSender,
    FakeDigestSender,
    Gap,
    SlackWebhook,
    build_digest,
    render,
    render_failure,
)
from invoice_collector.domain import (
    CollectionMonth,
    Email,
    EmailState,
    Extraction,
    ModelUsage,
    SummaryRow,
)
from invoice_collector.evals.answer_key import answer_key
from invoice_collector.ledger import CollectedDocument, Ledger
from invoice_collector.run import RunResult

AUGUST = CollectionMonth(2026, 8)
OPS = "ops@nyayalabs.example"


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[Ledger]:
    opened = Ledger(tmp_path / "ledger.sqlite")
    yield opened
    opened.close()


def _email(number: int, subject: str, account: str = OPS) -> Email:
    return Email(
        source_account=account,
        message_id=f"<{number}@vendor.example>",
        sender="billing@vendor.example",
        subject=subject,
        received_at=datetime(2026, 8, 5, 9, 0, tzinfo=UTC),
    )


def _extraction(vendor: str, total: str, currency: str = "USD") -> Extraction:
    return Extraction(
        document_type="invoice",
        vendor=vendor,
        invoice_date=date(2026, 8, 5),
        total=Decimal(total),
        currency=currency,
    )


def _row(vendor: str, total: str, rate: str | None, currency: str = "USD") -> SummaryRow:
    return SummaryRow(
        vendor=vendor,
        document_type="invoice",
        invoice_date=date(2026, 8, 5),
        total=Decimal(total),
        currency=currency,
        source_accounts=(OPS,),
        file_link=f"archive/{vendor}.pdf",
        inr_rate=Decimal(rate) if rate is not None else None,
    )


def _collected(ledger: Ledger, number: int, vendor: str, total: str) -> None:
    ledger.record(
        AUGUST,
        _email(number, f"Your {vendor} invoice"),
        EmailState.COLLECTED,
        documents=(CollectedDocument(f"hash-{number}", _extraction(vendor, total), "link"),),
    )


def _text(message: dict[str, Any]) -> str:
    """Everything a reader sees in the message's blocks, as one text."""
    parts: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():  # pyright: ignore[reportUnknownVariableType]
                if key == "text" and isinstance(item, str):
                    parts.append(item)
                else:
                    walk(item)
        elif isinstance(value, list):
            for item in value:  # pyright: ignore[reportUnknownVariableType]
                walk(item)

    walk(message["blocks"])
    return "\n".join(parts)


def _busy_month(ledger: Ledger) -> RunResult:
    _collected(ledger, 1, "Figma", "190.00")
    _collected(ledger, 2, "Slack", "1250.00")
    ledger.record(
        AUGUST,
        _email(3, "Your Zoom invoice is ready"),
        EmailState.NEEDS_REVIEW,
        reason="manual download needed",
    )
    ledger.record(AUGUST, _email(4, "Team offsite"), EmailState.SKIPPED, reason="not billing")
    ledger.record(AUGUST, _email(5, "Newsletter"), EmailState.SKIPPED, reason="not billing")
    ledger.record(
        AUGUST, _email(6, "Your AWS bill"), EmailState.FAILED, reason="PDF could not be read"
    )
    return RunResult(
        summary=[_row("Figma", "190.00", "83.50"), _row("Slack", "1250.00", None)],
        warnings=["Slack 2026-08-05: no rate for USD on 2026-08-05"],
    )


def test_headline_numbers_for_a_busy_month(ledger: Ledger) -> None:
    digest = build_digest(AUGUST, _busy_month(ledger), ledger)

    assert digest.month == AUGUST
    assert digest.documents_collected == 2
    assert digest.rupee_total == Decimal("15865.00")
    assert digest.charges_without_rupees == 1
    assert (digest.needs_review, digest.skipped, digest.failed) == (1, 2, 1)
    assert digest.warnings == ("Slack 2026-08-05: no rate for USD on 2026-08-05",)

    message = render(digest)
    headline = message["blocks"][1]["text"]["text"]
    assert "2026-08" in message["blocks"][0]["text"]["text"]
    assert "*2* billing documents collected" in headline
    assert "₹15,865.00" in headline
    assert "1 charge has no rupee amount" in headline
    assert "*1* need review" in headline
    assert "*2* skipped" in headline
    assert "*1* failed" in headline
    assert "2 billing documents collected" in message["text"]


def test_busy_month_lists_what_needs_review_failures_and_warnings(ledger: Ledger) -> None:
    text = _text(render(build_digest(AUGUST, _busy_month(ledger), ledger)))

    assert "Your Zoom invoice is ready" in text
    assert "manual download needed" in text
    assert "Your AWS bill" in text
    assert "PDF could not be read" in text
    assert "no rate for USD" in text
    assert "Nothing needs attention" not in text


def test_quiet_month_says_so_in_one_line(ledger: Ledger) -> None:
    _collected(ledger, 1, "Figma", "190.00")
    result = RunResult(summary=[_row("Figma", "190.00", "83.50")])

    message = render(build_digest(AUGUST, result, ledger, gaps=[]))

    assert len(message["blocks"]) == 3
    assert "Nothing needs attention" in message["blocks"][2]["text"]["text"]
    assert "Nothing needs attention" in message["text"]


def test_gaps_are_shown_with_their_explanations(ledger: Ledger) -> None:
    gaps: list[Gap] = [
        ("Notion", "missing", "payment failed on 2026-08-03"),
        ("Linear", "unknown", None),
    ]

    text = _text(render(build_digest(AUGUST, RunResult([]), ledger, gaps=gaps)))

    assert "*2* gaps" in text
    assert "Notion: missing (payment failed on 2026-08-03)" in text
    assert "Linear: unknown" in text


@pytest.mark.parametrize(
    ("usage", "said"),
    [
        (
            (
                ModelUsage("claude-haiku-4-5", 5, 11825, 285, Decimal("0.013250")),
                ModelUsage("jev-latest", 8, 1696, 64, Decimal("0.000071232")),
            ),
            "model cost $0.0133",
        ),
        ((), "model cost $0 (no model was called)"),
        (None, "model cost not recorded"),
        (
            (ModelUsage("jev-2-preview", 8, 1696, 64, None),),
            "model cost unknown: the cost of jev-2-preview is not known",
        ),
    ],
)
def test_digest_says_what_the_run_s_model_calls_cost(
    ledger: Ledger, usage: tuple[ModelUsage, ...] | None, said: str
) -> None:
    result = RunResult([], model_usage=usage)

    message = render(build_digest(AUGUST, result, ledger, gaps=[]))

    assert said in message["blocks"][1]["text"]["text"]
    assert said in message["text"]


def test_gaps_not_checked_differs_from_no_gaps(ledger: Ledger) -> None:
    not_checked = render(build_digest(AUGUST, RunResult([]), ledger, gaps=None))
    none_found = render(build_digest(AUGUST, RunResult([]), ledger, gaps=[]))

    assert "gaps not checked" in _text(not_checked)
    assert "no gaps" not in _text(not_checked)
    assert "no gaps" in _text(none_found)
    assert "not checked" not in _text(none_found)


def test_a_source_account_that_failed_to_sync_is_named(ledger: Ledger) -> None:
    digest = build_digest(
        AUGUST, RunResult([]), ledger, gaps=[], failed_source_accounts=["finance@nyayalabs.example"]
    )

    text = _text(render(digest))

    assert "finance@nyayalabs.example" in text
    assert "Nothing needs attention" not in text


def test_values_from_emails_are_escaped(ledger: Ledger) -> None:
    ledger.record(
        AUGUST,
        _email(1, "<!channel> & Co invoice"),
        EmailState.NEEDS_REVIEW,
        reason="amount <unclear> & odd",
    )
    gaps: list[Gap] = [("<!channel> & Co", "missing", "renewal <soon>")]

    message = render(build_digest(AUGUST, RunResult([]), ledger, gaps=gaps))
    everything = json.dumps(message, ensure_ascii=False)

    assert "<!channel>" not in everything
    assert "&lt;!channel&gt; &amp; Co" in _text(message)
    assert "amount &lt;unclear&gt; &amp; odd" in _text(message)
    assert "renewal &lt;soon&gt;" in _text(message)


def test_long_lists_are_cut_with_and_n_more(ledger: Ledger) -> None:
    for number in range(8):
        ledger.record(
            AUGUST, _email(number, f"Invoice {number}"), EmailState.NEEDS_REVIEW, reason="unclear"
        )

    text = _text(render(build_digest(AUGUST, RunResult([]), ledger)))

    assert "Invoice 4" in text
    assert "Invoice 5" not in text
    assert "and 3 more" in text


def test_limits_are_respected_for_a_month_with_hundreds_of_items(ledger: Ledger) -> None:
    long = "x" * 400
    for number in range(300):
        ledger.record(
            AUGUST, _email(number, f"Invoice {number} {long}"), EmailState.FAILED, reason=long
        )
        ledger.record(
            AUGUST,
            _email(1000 + number, f"Review {number} {long}"),
            EmailState.NEEDS_REVIEW,
            reason=long,
        )
    gaps: list[Gap] = [(f"Vendor {n} {long}", "missing", long) for n in range(300)]
    result = RunResult([], warnings=[f"warning {n} {long}" for n in range(300)])
    digest = build_digest(
        AUGUST, result, ledger, gaps=gaps, failed_source_accounts=[f"a{n}@x" for n in range(300)]
    )

    message = render(digest)

    assert len(message["blocks"]) <= 50
    texts: list[str] = []
    for block in message["blocks"]:
        if "text" in block:
            texts.append(block["text"]["text"])
        for element in block.get("elements", []):
            texts.append(element["text"])
    assert all(len(text) <= 3000 for text in texts)
    assert len(message["text"]) <= 3000
    assert "cut short" in _text(message)


@pytest.mark.parametrize(
    ("amount", "shown"),
    [
        ("0", "₹0.00"),
        ("999.5", "₹999.50"),
        ("1000", "₹1,000.00"),
        ("123456", "₹1,23,456.00"),
        ("12345678.9", "₹1,23,45,678.90"),
        ("-4500", "-₹4,500.00"),
    ],
)
def test_rupees_use_indian_digit_grouping(amount: str, shown: str) -> None:
    digest = Digest(month=AUGUST, rupee_total=Decimal(amount))

    assert shown in render(digest)["blocks"][1]["text"]["text"]


def test_links_go_to_the_summary_and_the_review_screen(ledger: Ledger) -> None:
    digest = build_digest(
        AUGUST,
        RunResult([]),
        ledger,
        summary_link="https://docs.google.com/spreadsheets/d/abc",
        dashboard_url="https://invoices.nyayalabs.example/",
    )

    text = _text(render(digest))

    assert "<https://docs.google.com/spreadsheets/d/abc|Summary>" in text
    assert "<https://invoices.nyayalabs.example/review?month=2026-08|Review>" in text


def test_a_summary_that_is_a_local_file_is_named_not_linked(ledger: Ledger) -> None:
    digest = build_digest(AUGUST, RunResult([]), ledger, summary_link="out/2026-08_summary.csv")

    text = _text(render(digest))

    assert "out/2026-08_summary.csv" in text
    assert "<out/" not in text


def test_failed_run_is_reported() -> None:
    message = render_failure(AUGUST, "Gmail sign-in expired for <ops> & co")

    assert "2026-08" in message["blocks"][0]["text"]["text"]
    assert "failed" in message["text"]
    assert "Gmail sign-in expired for &lt;ops&gt; &amp; co" in _text(message)


@dataclass
class Recorded:
    url: str
    paths: list[str] = field(default_factory=list[str])
    content_types: list[str] = field(default_factory=list[str])
    bodies: list[Any] = field(default_factory=list[Any])


Webhook = Callable[[int, bytes], Recorded]


@pytest.fixture
def webhook() -> Iterator[Webhook]:
    """Starts a local server standing in for Slack, recording what is posted to it."""
    servers: list[ThreadingHTTPServer] = []

    def start(status: int, answer: bytes) -> Recorded:
        recorded = Recorded(url="")

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                pass

            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers["Content-Length"]))
                recorded.paths.append(self.path)
                recorded.content_types.append(self.headers["Content-Type"])
                recorded.bodies.append(json.loads(body))
                self.send_response(status)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(answer)))
                self.end_headers()
                self.wfile.write(answer)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        recorded.url = f"http://127.0.0.1:{server.server_port}/services/T000/B000/secret-token"
        return recorded

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def test_webhook_posts_the_message_as_json(webhook: Webhook) -> None:
    slack = webhook(200, b"ok")
    message = render_failure(AUGUST, "boom")

    SlackWebhook(slack.url, allow_any_host=True, timeout=5).send(message)

    assert slack.paths == ["/services/T000/B000/secret-token"]
    assert slack.content_types == ["application/json"]
    assert slack.bodies == [message]


@pytest.mark.parametrize(
    "url",
    [
        "http://hooks.slack.com/services/T000/B000/x",
        "https://hooks.slack.com.attacker.example/services/x",
        "https://example.com/services/x",
        "not a url",
    ],
)
def test_a_webhook_that_is_not_slack_is_refused(url: str) -> None:
    with pytest.raises(ValueError) as refused:
        SlackWebhook(url)

    assert url not in str(refused.value)


def test_a_slack_webhook_is_accepted() -> None:
    SlackWebhook("https://hooks.slack.com/services/T000/B000/secret-token")


def test_an_error_status_is_not_sent_and_keeps_the_url_secret(webhook: Webhook) -> None:
    slack = webhook(404, b"no_service")

    with pytest.raises(DigestNotSent) as not_sent:
        SlackWebhook(slack.url, allow_any_host=True, timeout=5).send({"text": "hello"})

    reason = str(not_sent.value)
    assert "404" in reason
    assert "no_service" in reason
    assert "secret-token" not in reason
    assert not_sent.value.__cause__ is None
    assert not_sent.value.__context__ is None


def test_a_webhook_that_cannot_be_reached_is_not_sent() -> None:
    sender = SlackWebhook(
        "http://127.0.0.1:9/services/secret-token", allow_any_host=True, timeout=2
    )

    with pytest.raises(DigestNotSent, match="could not be reached") as not_sent:
        sender.send({"text": "hello"})

    assert "secret-token" not in str(not_sent.value)
    assert "secret-token" not in repr(sender)


def test_fake_sender_records_messages() -> None:
    sender = FakeDigestSender()

    sender.send({"text": "hello"})

    assert sender.sent == [{"text": "hello"}]


WEBHOOK_VARIABLE = "INVOICE_COLLECTOR_SLACK_WEBHOOK"
SLACK_URL = "https://hooks.slack.com/services/T000/B000/secret-token"
PDF = b"%PDF-1.7 figma invoice"
OFFLINE = ["--classifier", "rules", "--no-exchange-rates"]


def write_samples(root: Path) -> None:
    message = EmailMessage()
    message["From"] = "Figma <billing@figma.com>"
    message["Subject"] = "Your Figma invoice"
    message["Date"] = format_datetime(datetime(2026, 8, 21, 6, 5, tzinfo=UTC))
    message["Message-ID"] = "<figma-1@figma.com>"
    message.set_content("Your invoice is attached.")
    message.add_attachment(PDF, maintype="application", subtype="pdf", filename="invoice.pdf")
    account = root / OPS
    account.mkdir(parents=True)
    (account / "figma.eml").write_bytes(bytes(message))
    answer = {
        "document_type": "invoice",
        "vendor": "Figma",
        "invoice_date": "2026-08-21",
        "total": "190.00",
        "currency": "USD",
    }
    answers = {hashlib.sha256(PDF).hexdigest(): answer}
    (root / "answers.json").write_text(json.dumps(answers), encoding="utf-8")


class Senders:
    """Stands in for the webhook the command line makes, recording what it is given."""

    def __init__(self, sender: DigestSender) -> None:
        self.sender = sender
        self.urls: list[str] = []

    def __call__(self, url: str) -> DigestSender:
        self.urls.append(url)
        return self.sender


class RefusingSender:
    def send(self, message: dict[str, Any]) -> None:
        raise DigestNotSent("Slack refused the digest: HTTP 404 (no_service)")


def _collect(tmp_path: Path, senders: Callable[[str], DigestSender], *extra: str) -> int:
    samples = tmp_path / "samples"
    if not samples.exists():
        write_samples(samples)
    argv = ["collect", "2026-08", "--samples", str(samples), "--out", str(tmp_path / "out")]
    return main([*argv, *OFFLINE, *extra], digest_sender_for=senders, extractor=answer_key(samples))


def test_command_line_sends_a_digest_when_the_webhook_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(WEBHOOK_VARIABLE, SLACK_URL)
    fake = FakeDigestSender()
    senders = Senders(fake)

    assert _collect(tmp_path, senders) == 0

    assert senders.urls == [SLACK_URL]
    [message] = fake.sent
    assert "2026-08" in message["blocks"][0]["text"]["text"]
    assert "1 billing document collected" in message["text"]
    assert "2026-08_summary.csv" in _text(message)


def test_the_digest_links_to_the_dashboard_at_its_public_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(WEBHOOK_VARIABLE, SLACK_URL)
    monkeypatch.setenv("INVOICE_COLLECTOR_PUBLIC_URL", "https://invoices.example.com")
    fake = FakeDigestSender()

    assert _collect(tmp_path, Senders(fake)) == 0

    [message] = fake.sent
    assert "<https://invoices.example.com/review?month=2026-08|Review>" in _text(message)


def test_a_public_address_that_is_not_one_stops_the_run_before_it_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("INVOICE_COLLECTOR_PUBLIC_URL", "invoices.example.com")

    assert _collect(tmp_path, Senders(FakeDigestSender())) == 2

    assert "INVOICE_COLLECTOR_PUBLIC_URL must be the address" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_command_line_sends_nothing_when_the_webhook_is_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(WEBHOOK_VARIABLE, raising=False)
    senders = Senders(FakeDigestSender())

    assert _collect(tmp_path, senders) == 0

    assert senders.urls == []
    output = capsys.readouterr().out
    assert "Slack" not in output
    assert "digest" not in output.lower()


def test_no_digest_option_skips_sending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(WEBHOOK_VARIABLE, SLACK_URL)
    fake = FakeDigestSender()

    assert _collect(tmp_path, Senders(fake), "--no-digest") == 0

    assert fake.sent == []


def test_a_digest_that_cannot_be_sent_keeps_the_run_successful(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(WEBHOOK_VARIABLE, SLACK_URL)

    assert _collect(tmp_path, Senders(RefusingSender())) == 0

    output = capsys.readouterr().out
    assert "Warning: digest not sent: Slack refused the digest: HTTP 404" in output
    assert "secret-token" not in output


def test_a_webhook_that_is_not_slack_is_refused_with_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(WEBHOOK_VARIABLE, "https://attacker.example/secret-token")

    assert _collect(tmp_path, SlackWebhook) == 0

    output = capsys.readouterr().out
    assert "Warning: digest not sent" in output
    assert "secret-token" not in output


def test_a_failed_run_is_reported_and_still_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(WEBHOOK_VARIABLE, SLACK_URL)
    fake = FakeDigestSender()
    write_samples(tmp_path / "samples")
    # An email with no date cannot be read, so the run stops before examining any.
    (tmp_path / "samples" / OPS / "undated.eml").write_text("Subject: hi\n\nbody\n")

    with pytest.raises(ValueError, match="has no Date header"):
        _collect(tmp_path, Senders(fake))

    [message] = fake.sent
    assert "Run failed for collection month 2026-08" in message["blocks"][0]["text"]["text"]
    assert "has no Date header" in message["text"]


OWNER = "finance@nyayalabs.example"


def test_a_run_whose_owner_account_is_not_signed_in_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(WEBHOOK_VARIABLE, SLACK_URL)
    fake = FakeDigestSender()
    owner = ["--google-owner", OWNER, "--token-dir", str(tmp_path / "tokens")]

    assert _collect(tmp_path, Senders(fake), *owner) == 1

    [message] = fake.sent
    assert "Run failed for collection month 2026-08" in message["blocks"][0]["text"]["text"]
    assert f"the owner account {OWNER} is not signed in to Google" in message["text"]
