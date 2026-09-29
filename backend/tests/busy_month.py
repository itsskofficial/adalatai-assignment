"""A month with several emails in several source accounts, some of which fail.

Shared by the tests that examine emails concurrently, so each compares against the
same collection examined one email after another.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from invoice_collector.archive import LocalArchive
from invoice_collector.classifier import Classifier, FakeClassifier
from invoice_collector.domain import Attachment, CollectionMonth, Email, Extraction
from invoice_collector.exchange_rates import FakeExchangeRates
from invoice_collector.extractor import FakeExtractor
from invoice_collector.ledger import Ledger
from invoice_collector.mail_source import InMemoryMailSource, MailSource
from invoice_collector.portal import FakePortalFetcher
from invoice_collector.renderer import FakeRenderer
from invoice_collector.run import Pipeline

AUGUST = CollectionMonth(2026, 8)
ENGINEERING = "engineering@nyayalabs.example"
OPS = "ops@nyayalabs.example"
FINANCE = "finance@nyayalabs.example"

SLACK_PDF = b"%PDF-1.7 slack invoice"
ZOOM_PDF = b"%PDF-1.7 zoom invoice"
LINEAR_PDF = b"%PDF-1.7 linear invoice"
UNREADABLE_PDF = b"%PDF-1.7 a scan nobody can read"
NOTION_HTML = "<h2>Receipt #2391-7745</h2><p>Total paid 221.40 EUR</p>"
FIGMA_PORTAL = "https://billing.figma.example/i/in_1PqX7fK2"
BROKEN_PORTAL = "https://gone.example/invoice/1"
FIGMA_PDF = b"%PDF-1.7 figma invoice from the portal"

EXTRACTIONS = {
    SLACK_PDF: Extraction("invoice", "Slack", date(2026, 8, 3), Decimal("652.50"), "USD"),
    ZOOM_PDF: Extraction("invoice", "Zoom", date(2026, 8, 9), Decimal("149.90"), "USD"),
    LINEAR_PDF: Extraction("invoice", "Linear", date(2026, 8, 12), Decimal("80.00"), "USD"),
    # No rate is known for euros, so collecting this one gives a warning.
    FakeRenderer().render_html(NOTION_HTML): Extraction(
        "receipt", "Notion", date(2026, 8, 14), Decimal("221.40"), "EUR"
    ),
    FIGMA_PDF: Extraction("invoice", "Figma", date(2026, 8, 21), Decimal("190.00"), "USD"),
}


def _at(day: int) -> datetime:
    return datetime(2026, 8, day, 9, 0, tzinfo=UTC)


def _attached(account: str, message_id: str, vendor: str, pdf: bytes, day: int) -> Email:
    return Email(
        source_account=account,
        message_id=message_id,
        sender=f"{vendor} <billing@{vendor.lower()}.example>",
        subject=f"Your {vendor} invoice",
        received_at=_at(day),
        attachments=(Attachment("invoice.pdf", "application/pdf", pdf),),
    )


def _portal(account: str, message_id: str, url: str, day: int) -> Email:
    return Email(
        source_account=account,
        message_id=message_id,
        sender="Billing <billing@portal.example>",
        subject=f"Your invoice is ready ({message_id})",
        received_at=_at(day),
        html_body=f'<p>Your latest invoice is ready.</p><a href="{url}">View invoice</a>',
    )


EMAILS = [
    _attached(ENGINEERING, "m-slack", "Slack", SLACK_PDF, 3),
    _attached(ENGINEERING, "m-zoom", "Zoom", ZOOM_PDF, 9),
    # Cannot be classified: fails with a reason.
    _attached(ENGINEERING, "m-unclassified", "Asana", b"%PDF-1.7 asana", 10),
    Email(
        source_account=ENGINEERING,
        message_id="m-news",
        sender="Slack <news@slack.com>",
        subject="What is new in Slack",
        received_at=_at(10),
    ),
    _attached(OPS, "m-linear", "Linear", LINEAR_PDF, 12),
    # Nothing can read it: fails with a reason.
    _attached(OPS, "m-unreadable", "Miro", UNREADABLE_PDF, 13),
    Email(
        source_account=OPS,
        message_id="m-notion",
        sender="Notion <team@mail.notion.so>",
        subject="Your receipt from Notion #2391-7745",
        received_at=_at(14),
        html_body=NOTION_HTML,
    ),
    _portal(OPS, "m-figma", FIGMA_PORTAL, 21),
    # The portal cannot be opened: fails with a reason.
    _portal(OPS, "m-broken", BROKEN_PORTAL, 22),
]


@dataclass(frozen=True)
class BusyMonth:
    """One collection of the busy month, writing to its own folder."""

    folder: Path
    ledger: Ledger

    @staticmethod
    def sources(emails: list[Email] | None = None) -> list[MailSource]:
        emails = EMAILS if emails is None else emails
        return [
            InMemoryMailSource(ENGINEERING, [e for e in emails if e.source_account == ENGINEERING]),
            InMemoryMailSource(OPS, [e for e in emails if e.source_account == OPS]),
            InMemoryMailSource(FINANCE, [], unavailable="the sign-in has expired"),
        ]

    def pipeline(self, classifier: Classifier | None = None) -> Pipeline:
        return Pipeline(
            classifier=classifier or FakeClassifier(failing=frozenset({"m-unclassified"})),
            extractor=FakeExtractor.for_documents(EXTRACTIONS),
            renderer=FakeRenderer(),
            portal_fetcher=FakePortalFetcher({FIGMA_PORTAL: FIGMA_PDF}),
            exchange_rates=FakeExchangeRates({"USD": Decimal("95.34")}),
            archive=LocalArchive(self.folder / "archive"),
            ledger=self.ledger,
        )

    def states(self) -> dict[str, tuple[str, str | None]]:
        """The state and reason of each email examined, by message id."""
        return {
            e.message_id: (e.state.value, e.reason) for e in self.ledger.examined_emails(AUGUST)
        }
