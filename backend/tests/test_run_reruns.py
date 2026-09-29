"""Tests at the run seam: what a failure, or a second run, may and may not change."""

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from support import real_pdf

from invoice_collector.archive import LocalArchive
from invoice_collector.classifier import FakeClassifier
from invoice_collector.domain import (
    Attachment,
    Classification,
    CollectionMonth,
    Email,
    EmailState,
    Extraction,
)
from invoice_collector.exchange_rates import FakeExchangeRates
from invoice_collector.extractor import FakeExtractor
from invoice_collector.ledger import Ledger
from invoice_collector.mail_source import InMemoryMailSource
from invoice_collector.portal import FakePortalFetcher, LoginGated
from invoice_collector.renderer import FakeRenderer, RenderFailed
from invoice_collector.run import Pipeline, RunResult, collect

AUGUST = CollectionMonth(2026, 8)
OPS = "ops@nyayalabs.example"
PORTAL = "https://billing.figma.example/invoice/in_1PqX7fK2"
# PDFs that open, so one that no reader can read fails rather than being held.
SLACK_PDF, FIGMA_PDF = real_pdf("slack invoice"), real_pdf("figma invoice")
SLACK = Extraction("invoice", "Slack", date(2026, 8, 3), Decimal("652.50"), "USD")
FIGMA = Extraction("invoice", "Figma", date(2026, 8, 21), Decimal("190.00"), "USD")
BROKEN_BODY = "<p>Receipt. Total paid $96.00</p><img src='cid:broken'>"


class RendererThatBreaks:
    def render_html(self, html: str) -> bytes:
        if html == BROKEN_BODY:
            raise RenderFailed("the page could not be rendered")
        return FakeRenderer().render_html(html)


@dataclass
class World:
    """What a run sees. A test changes it between two runs."""

    tmp_path: Path
    ledger: Ledger
    classifier: FakeClassifier = field(default_factory=FakeClassifier)
    answers: dict[bytes, Extraction] = field(
        default_factory=lambda: {SLACK_PDF: SLACK, FIGMA_PDF: FIGMA}
    )
    pages: dict[str, bytes | LoginGated] = field(default_factory=lambda: {PORTAL: FIGMA_PDF})

    def run(self, emails: list[Email]) -> RunResult:
        return collect(
            AUGUST,
            sources=[InMemoryMailSource(OPS, emails)],
            pipeline=Pipeline(
                classifier=self.classifier,
                extractor=FakeExtractor.for_documents(self.answers),
                renderer=RendererThatBreaks(),
                portal_fetcher=FakePortalFetcher(self.pages),
                exchange_rates=FakeExchangeRates({"USD": Decimal("95.34")}),
                archive=LocalArchive(self.tmp_path / "archive"),
                ledger=self.ledger,
            ),
            summary_writers=[],
        )

    def state_of(self, message_id: str) -> tuple[EmailState, str | None]:
        [email] = [e for e in self.ledger.examined_emails(AUGUST) if e.message_id == message_id]
        return email.state, email.reason


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    ledger = Ledger(tmp_path / "ledger.sqlite")
    yield World(tmp_path, ledger)
    ledger.close()


def email(message_id: str, subject: str, day: int, **content: object) -> Email:
    return Email(
        source_account=OPS,
        message_id=message_id,
        sender="Billing <billing@vendor.example>",
        subject=subject,
        received_at=datetime(2026, 8, day, 9, 0, tzinfo=UTC),
        **content,  # pyright: ignore[reportArgumentType]
    )


def slack_invoice() -> Email:
    attachment = Attachment("invoice.pdf", "application/pdf", SLACK_PDF)
    return email("m-slack", "Your Slack invoice", 3, attachments=(attachment,))


def figma_link() -> Email:
    body = f'<p>Your invoice is ready.</p><a href="{PORTAL}">View invoice</a>'
    return email("m-figma", "Your Figma invoice is ready", 21, html_body=body)


def test_email_body_that_cannot_be_rendered_fails_without_stopping_the_run(world: World) -> None:
    broken = email("m-broken", "Your receipt", 9, html_body=BROKEN_BODY)

    result = world.run([broken, slack_invoice()])

    assert [row.vendor for row in result.summary] == ["Slack"]
    assert world.state_of("m-broken") == (EmailState.FAILED, "the page could not be rendered")


def test_portal_link_that_stops_answering_keeps_the_invoice_collected_before(
    world: World,
) -> None:
    world.run([figma_link()])
    world.pages.clear()

    result = world.run([figma_link()])

    assert [row.vendor for row in result.summary] == ["Figma"]
    assert world.state_of("m-figma") == (EmailState.COLLECTED, None)


def test_portal_link_that_now_asks_for_a_sign_in_keeps_the_invoice_collected_before(
    world: World,
) -> None:
    world.run([figma_link()])
    world.pages[PORTAL] = LoginGated()

    result = world.run([figma_link()])

    assert [row.vendor for row in result.summary] == ["Figma"]


def test_classifier_that_fails_on_a_second_run_keeps_what_was_collected(world: World) -> None:
    world.run([slack_invoice()])
    world.classifier = FakeClassifier(failing=frozenset({"m-slack"}))

    result = world.run([slack_invoice()])

    assert [row.vendor for row in result.summary] == ["Slack"]
    assert world.state_of("m-slack") == (EmailState.COLLECTED, None)
    assert result.warnings == [
        "Your Slack invoice: kept what was collected before "
        "(this run: the classifier is unavailable)"
    ]


def test_classifier_that_changes_its_mind_keeps_what_was_collected(world: World) -> None:
    world.run([slack_invoice()])
    world.classifier = FakeClassifier({"m-slack": Classification("not_billing", None, "high")})

    result = world.run([slack_invoice()])

    assert [row.vendor for row in result.summary] == ["Slack"]
    assert result.warnings == [
        "Your Slack invoice: kept what was collected before (this run: not a billing email)"
    ]


def test_email_that_failed_before_is_collected_once_it_can_be_read(world: World) -> None:
    world.answers.clear()
    world.run([slack_invoice()])
    assert world.state_of("m-slack")[0] is EmailState.FAILED
    world.answers[SLACK_PDF] = SLACK

    result = world.run([slack_invoice()])

    assert [row.vendor for row in result.summary] == ["Slack"]
    assert world.state_of("m-slack") == (EmailState.COLLECTED, None)
