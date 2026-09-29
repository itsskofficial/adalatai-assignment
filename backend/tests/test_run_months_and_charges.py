"""Tests at the run seam: collection months, charges, and amounts in rupees."""

from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from invoice_collector.archive import LocalArchive
from invoice_collector.classifier import FakeClassifier
from invoice_collector.domain import CollectionMonth, Email, EmailState, Extraction
from invoice_collector.exchange_rates import FakeExchangeRates
from invoice_collector.extractor import FakeExtractor
from invoice_collector.ledger import Ledger
from invoice_collector.mail_source import InMemoryMailSource
from invoice_collector.portal import FakePortalFetcher
from invoice_collector.run import Pipeline, RunResult, Settings, collect

JULY, AUGUST, SEPTEMBER = (CollectionMonth(2026, m) for m in (7, 8, 9))
ENGINEERING = "engineering@nyayalabs.example"
OPS = "ops@nyayalabs.example"
FINANCE = "finance@nyayalabs.example"


class CountingRenderer:
    """Like a real browser, it never produces the same bytes twice."""

    def __init__(self) -> None:
        self.rendered = 0

    def render_html(self, html: str) -> bytes:
        self.rendered += 1
        return f"%PDF-rendered {self.rendered} {html}".encode()


@dataclass
class Collection:
    tmp_path: Path
    ledger: Ledger
    answers: dict[bytes, Extraction] = field(default_factory=dict[bytes, Extraction])
    rates: FakeExchangeRates = field(
        default_factory=lambda: FakeExchangeRates({"USD": Decimal("95.34")})
    )
    renderer: CountingRenderer = field(default_factory=CountingRenderer)

    def run(
        self, emails: list[Email], month: CollectionMonth = AUGUST, window_days: int = 7
    ) -> RunResult:
        accounts = sorted({email.source_account for email in emails})
        return collect(
            month,
            sources=[
                InMemoryMailSource(a, [e for e in emails if e.source_account == a])
                for a in accounts
            ],
            pipeline=Pipeline(
                classifier=FakeClassifier(),
                extractor=FakeExtractor.for_documents(self.answers),
                renderer=self.renderer,
                portal_fetcher=FakePortalFetcher({}),
                exchange_rates=self.rates,
                archive=LocalArchive(self.tmp_path / "archive"),
                ledger=self.ledger,
            ),
            summary_writers=[],
            settings=Settings(search_window_days=window_days),
        )

    def saved_files(self, month: CollectionMonth = AUGUST) -> list[str]:
        folder = self.tmp_path / "archive" / str(month)
        return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


@pytest.fixture
def collection(tmp_path: Path) -> Iterator[Collection]:
    ledger = Ledger(tmp_path / "ledger.sqlite")
    yield Collection(tmp_path, ledger)
    ledger.close()


def invoice_email(
    vendor: str,
    pdf: bytes,
    *,
    received: datetime,
    account: str = ENGINEERING,
    subject: str | None = None,
) -> Email:
    from invoice_collector.domain import Attachment

    return Email(
        source_account=account,
        message_id=f"m-{vendor.lower()}-{account.split('@')[0]}-{received:%m%d}",
        sender=f"{vendor} <billing@{vendor.lower()}.example>",
        subject=subject or f"Your {vendor} invoice",
        received_at=received,
        attachments=(Attachment("invoice.pdf", "application/pdf", pdf),),
    )


def usd(vendor: str, day: date, total: str, document_type: str = "invoice") -> Extraction:
    return Extraction(document_type, vendor, day, Decimal(total), "USD")  # pyright: ignore[reportArgumentType]


# Collection months


def test_invoice_dated_in_the_month_but_emailed_just_after_it_is_collected(
    collection: Collection,
) -> None:
    pdf = b"%PDF-1.7 aws august"
    collection.answers[pdf] = usd("AWS", date(2026, 8, 31), "1840.22")
    late = invoice_email("AWS", pdf, received=datetime(2026, 9, 2, 3, 0, tzinfo=UTC))

    result = collection.run([late])

    assert [row.vendor for row in result.summary] == ["AWS"]
    assert collection.saved_files() == ["2026-08_AWS_1840.22-USD.pdf"]


def test_invoice_emailed_in_the_month_but_dated_in_another_is_left_for_that_month(
    collection: Collection,
) -> None:
    pdf = b"%PDF-1.7 aws july"
    collection.answers[pdf] = usd("AWS", date(2026, 7, 31), "1712.09")
    early_august = invoice_email("AWS", pdf, received=datetime(2026, 8, 2, 3, 0, tzinfo=UTC))

    result = collection.run([early_august])

    assert result.summary == []
    assert collection.saved_files() == []
    [examined] = collection.ledger.examined_emails(AUGUST)
    assert (examined.state, examined.reason) == (
        EmailState.SKIPPED,
        "belongs to collection month 2026-07",
    )


def test_that_invoice_is_collected_when_its_own_month_is_run(collection: Collection) -> None:
    pdf = b"%PDF-1.7 aws july"
    collection.answers[pdf] = usd("AWS", date(2026, 7, 31), "1712.09")
    early_august = invoice_email("AWS", pdf, received=datetime(2026, 8, 2, 3, 0, tzinfo=UTC))
    collection.run([early_august], AUGUST)

    result = collection.run([early_august], JULY)

    assert [row.vendor for row in result.summary] == ["AWS"]
    assert collection.saved_files(JULY) == ["2026-07_AWS_1712.09-USD.pdf"]


def test_running_the_next_month_leaves_an_invoice_collected_for_this_one(
    collection: Collection,
) -> None:
    pdf = b"%PDF-1.7 aws august"
    collection.answers[pdf] = usd("AWS", date(2026, 8, 31), "1840.22")
    late = invoice_email("AWS", pdf, received=datetime(2026, 9, 2, 3, 0, tzinfo=UTC))
    collection.run([late], AUGUST)

    september = collection.run([late], SEPTEMBER)

    assert september.summary == []
    assert collection.ledger.examined_emails(SEPTEMBER) == []
    assert [d.extraction.vendor for d in collection.ledger.documents(AUGUST)] == ["AWS"]


def test_search_window_can_be_narrowed(collection: Collection) -> None:
    pdf = b"%PDF-1.7 aws august"
    collection.answers[pdf] = usd("AWS", date(2026, 8, 31), "1840.22")
    late = invoice_email("AWS", pdf, received=datetime(2026, 9, 2, 3, 0, tzinfo=UTC))

    result = collection.run([late], window_days=0)

    assert result.summary == []


def test_email_outside_the_month_with_nothing_to_collect_is_left_alone(
    collection: Collection,
) -> None:
    newsletter = Email(
        source_account=ENGINEERING,
        message_id="m-news-0902",
        sender="Slack <news@slack.com>",
        subject="What is new in Slack",
        received_at=datetime(2026, 9, 2, 8, 0, tzinfo=UTC),
    )

    collection.run([newsletter])

    assert collection.ledger.examined_emails(AUGUST) == []


# Charges


def test_same_document_in_two_source_accounts_is_one_row_and_one_file(
    collection: Collection,
) -> None:
    pdf = b"%PDF-1.7 github august"
    collection.answers[pdf] = usd("GitHub", date(2026, 8, 5), "210.00")
    received = datetime(2026, 8, 5, 6, 0, tzinfo=UTC)
    copies = [
        invoice_email("GitHub", pdf, received=received, account=ENGINEERING),
        invoice_email("GitHub", pdf, received=received, account=FINANCE),
    ]

    result = collection.run(copies)

    [row] = result.summary
    assert row.source_accounts == (ENGINEERING, FINANCE)
    assert row.source_account == f"{ENGINEERING}; {FINANCE}"
    assert collection.saved_files() == ["2026-08_GitHub_210.00-USD.pdf"]
    states = [e.state for e in collection.ledger.examined_emails(AUGUST)]
    assert states == [EmailState.COLLECTED, EmailState.COLLECTED]


def test_same_email_body_in_two_source_accounts_is_rendered_and_read_once(
    collection: Collection,
) -> None:
    body = "<h2>Receipt</h2><p>Total paid $96.00</p>"
    first_render = CountingRenderer().render_html(body)
    collection.answers[first_render] = usd("Linear", date(2026, 8, 27), "96.00", "receipt")
    received = datetime(2026, 8, 27, 6, 0, tzinfo=UTC)
    copies = [
        Email(account, f"m-linear-{account}", "Linear <billing@linear.app>", "Receipt for Linear",
              received, html_body=body)
        for account in (OPS, FINANCE)
    ]  # fmt: skip

    result = collection.run(copies)

    [row] = result.summary
    assert row.source_accounts == (FINANCE, OPS)
    assert collection.renderer.rendered == 1
    assert collection.saved_files() == ["2026-08_Linear_96.00-USD.pdf"]


def test_invoice_and_receipt_for_the_same_charge_are_one_row(collection: Collection) -> None:
    invoice_pdf, receipt_pdf = b"%PDF-1.7 zoom invoice", b"%PDF-1.7 zoom receipt"
    collection.answers[invoice_pdf] = usd("Zoom", date(2026, 8, 9), "149.90")
    collection.answers[receipt_pdf] = usd("Zoom", date(2026, 8, 11), "149.90", "receipt")
    emails = [
        invoice_email("Zoom", invoice_pdf, received=datetime(2026, 8, 9, 6, 0, tzinfo=UTC)),
        invoice_email(
            "Zoom",
            receipt_pdf,
            received=datetime(2026, 8, 11, 6, 0, tzinfo=UTC),
            account=OPS,
            subject="Your Zoom receipt",
        ),
    ]

    result = collection.run(emails)

    [row] = result.summary
    assert row.document_type == "invoice"
    assert row.file_link == "archive/2026-08/2026-08_Zoom_149.90-USD.pdf"
    assert row.source_accounts == (ENGINEERING, OPS)
    assert row.notes == "receipt also received: archive/2026-08/2026-08_Zoom_149.90-USD_2.pdf"


def test_receipt_for_a_different_amount_is_its_own_charge(collection: Collection) -> None:
    invoice_pdf, receipt_pdf = b"%PDF-1.7 zoom invoice", b"%PDF-1.7 zoom add-on receipt"
    collection.answers[invoice_pdf] = usd("Zoom", date(2026, 8, 9), "149.90")
    collection.answers[receipt_pdf] = usd("Zoom", date(2026, 8, 11), "40.00", "receipt")
    emails = [
        invoice_email("Zoom", invoice_pdf, received=datetime(2026, 8, 9, 6, 0, tzinfo=UTC)),
        invoice_email("Zoom", receipt_pdf, received=datetime(2026, 8, 11, 6, 0, tzinfo=UTC)),
    ]

    result = collection.run(emails)

    assert [(row.document_type, row.total) for row in result.summary] == [
        ("invoice", Decimal("149.90")),
        ("receipt", Decimal("40.00")),
    ]


def test_two_invoices_for_the_same_amount_stay_two_charges(collection: Collection) -> None:
    first, second = b"%PDF-1.7 figma invoice 1", b"%PDF-1.7 figma invoice 2"
    collection.answers[first] = usd("Figma", date(2026, 8, 1), "190.00")
    collection.answers[second] = usd("Figma", date(2026, 8, 21), "190.00")
    emails = [
        invoice_email("Figma", first, received=datetime(2026, 8, 1, 6, 0, tzinfo=UTC)),
        invoice_email("Figma", second, received=datetime(2026, 8, 21, 6, 0, tzinfo=UTC)),
    ]

    result = collection.run(emails)

    assert len(result.summary) == 2


# Rupees


def test_amount_is_also_shown_in_rupees_at_the_rate_on_the_invoice_date(
    collection: Collection,
) -> None:
    pdf = b"%PDF-1.7 slack august"
    collection.answers[pdf] = usd("Slack", date(2026, 8, 3), "652.50")

    result = collection.run(
        [invoice_email("Slack", pdf, received=datetime(2026, 8, 3, 9, 0, tzinfo=UTC))]
    )

    [row] = result.summary
    assert row.inr_rate == Decimal("95.34")
    assert row.inr_total == Decimal("62209.35")
    assert collection.rates.asked == [("USD", date(2026, 8, 3))]
    assert result.warnings == []


def test_stored_rate_is_not_fetched_again_when_the_month_is_run_again(
    collection: Collection,
) -> None:
    pdf = b"%PDF-1.7 slack august"
    collection.answers[pdf] = usd("Slack", date(2026, 8, 3), "652.50")
    emails = [invoice_email("Slack", pdf, received=datetime(2026, 8, 3, 9, 0, tzinfo=UTC))]
    collection.run(emails)

    result = collection.run(emails)

    assert len(collection.rates.asked) == 1
    assert result.summary[0].inr_rate == Decimal("95.34")


def test_missing_rate_leaves_the_rupee_amount_empty_and_is_reported(
    collection: Collection,
) -> None:
    pdf = b"%PDF-1.7 canva august"
    collection.answers[pdf] = replace(usd("Canva", date(2026, 8, 12), "119.00"), currency="AUD")

    result = collection.run(
        [invoice_email("Canva", pdf, received=datetime(2026, 8, 12, 9, 0, tzinfo=UTC))]
    )

    [row] = result.summary
    assert (row.total, row.currency, row.inr_total) == (Decimal("119.00"), "AUD", None)
    assert result.warnings == ["Canva 2026-08-12: no rate for AUD on 2026-08-12"]


def test_amount_already_in_rupees_needs_no_rate(collection: Collection) -> None:
    pdf = b"%PDF-1.7 zoho august"
    collection.answers[pdf] = replace(usd("Zoho", date(2026, 8, 7), "4720.00"), currency="INR")

    result = collection.run(
        [invoice_email("Zoho", pdf, received=datetime(2026, 8, 7, 9, 0, tzinfo=UTC))]
    )

    assert result.summary[0].inr_total == Decimal("4720.00")
    assert collection.rates.asked == []
