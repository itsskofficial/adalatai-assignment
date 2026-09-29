"""The digest sent to Slack after a run, checked without ever calling Slack."""

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from invoice_collector.digest import (
    Digest,
    Gap,
    build_digest,
    render,
    render_failure,
)
from invoice_collector.domain import CollectionMonth, Email, EmailState, Extraction, SummaryRow
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
