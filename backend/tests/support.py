"""What the tests at the run seam share: a collection that can be run, and sample emails."""

import io
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from pypdf import PdfWriter

from invoice_collector import tracing, trail
from invoice_collector.archive import Archive, LocalArchive
from invoice_collector.classifier import Classifier, FakeClassifier
from invoice_collector.domain import (
    Attachment,
    BillingCycle,
    Classification,
    CollectionMonth,
    DocumentType,
    Email,
    ExpectedVendor,
    Extraction,
)
from invoice_collector.exchange_rates import FakeExchangeRates
from invoice_collector.extractor import Extractor, FakeExtractor
from invoice_collector.ledger import Ledger
from invoice_collector.mail_source import InMemoryMailSource
from invoice_collector.portal import FakePortalFetcher, LoginGated
from invoice_collector.run import Pipeline, RunResult, Settings, collect
from invoice_collector.tracing import Tracer
from invoice_collector.vendor_matcher import RulesFirstVendorMatcher, VendorMatch, VendorMatcher

JULY, AUGUST, SEPTEMBER = (CollectionMonth(2026, m) for m in (7, 8, 9))
ENGINEERING = "engineering@nyayalabs.example"
OPS = "ops@nyayalabs.example"
FINANCE = "finance@nyayalabs.example"
# When a run in a test happens, unless the test moves the clock.
RUN_AT = datetime(2026, 9, 3, 6, 0, tzinfo=UTC)


HAIKU = "claude-haiku-4-5"
SONNET = "claude-sonnet-5-5"


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 1000
    output_tokens: int = 50


@dataclass(frozen=True)
class Answered[T]:
    """A fake's answer in the shape of a model's response: the answer, and its usage."""

    value: T
    usage: Usage = Usage()


class TracedClassifier:
    """A classifier whose calls are traced as Claude's adapter traces them."""

    def __init__(self, inner: Classifier, tracer: Tracer) -> None:
        self._inner, self._tracer = inner, tracer

    def classify(self, email: Email) -> Classification:
        return tracing.traced(
            self._tracer,
            tracing.CLASSIFICATION,
            HAIKU,
            "anthropic",
            lambda: Answered(self._inner.classify(email)),
            output=lambda answered: {"kind": answered.value.kind},
        )().value


class TracedExtractor:
    """An extractor whose calls are traced as Claude's adapter traces them."""

    def __init__(self, inner: Extractor, tracer: Tracer, model: str) -> None:
        self._inner, self._tracer, self._model = inner, tracer, model

    def extract(self, pdf: bytes) -> Extraction:
        return tracing.traced(
            self._tracer,
            tracing.EXTRACTION,
            self._model,
            "anthropic",
            lambda: Answered(self._inner.extract(pdf)),
            output=lambda answered: trail.fields_read(answered.value),
        )().value


class TracedVendorMatcher:
    """A vendor matcher whose calls are traced as Claude's adapter traces them."""

    def __init__(self, inner: VendorMatcher, tracer: Tracer) -> None:
        self._inner, self._tracer = inner, tracer

    def match(self, text: str, expected_vendors: Sequence[str]) -> VendorMatch:
        return tracing.traced(
            self._tracer,
            tracing.VENDOR_MATCHING,
            HAIKU,
            "anthropic",
            lambda: Answered(self._inner.match(text, expected_vendors)),
            output=lambda answered: {"vendor": answered.value.vendor},
        )().value


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
    # What the stronger model reads, when the first reading is doubted.
    stronger_answers: dict[bytes, Extraction] = field(default_factory=dict[bytes, Extraction])
    anomaly_threshold: Decimal = Decimal("0.30")
    # Source accounts that are read even when no email is given for them.
    source_accounts: tuple[str, ...] = ()
    # Source accounts that cannot be read, each with the reason.
    unavailable: dict[str, str] = field(default_factory=dict[str, str])
    # Matches a vendor to the expected vendors. None matches by rules alone.
    vendor_matcher: VendorMatcher | None = None
    # Where PDFs are kept. None keeps them in a local archive under tmp_path.
    archive: Archive | None = None
    # Classifies emails. None classifies by the fake's rules.
    classifier: Classifier | None = None
    # The time the run reads, for the history of each billing document.
    clock: list[datetime] = field(default_factory=lambda: [RUN_AT])
    # How long the run waits before each retry of an email. Tests do not wait.
    retry_delays: tuple[float, ...] = (0.0, 0.0)
    # What each portal link leads to. A link not given here cannot be opened.
    portal_pages: dict[str, bytes | LoginGated] = field(
        default_factory=dict[str, bytes | LoginGated]
    )
    # Traces each call to the classifier, the extractors and the vendor matcher, as a
    # model's adapter would. None traces nothing.
    tracer: Tracer | None = None

    def run(
        self, emails: list[Email], month: CollectionMonth = AUGUST, window_days: int = 7
    ) -> RunResult:
        accounts = sorted(
            {email.source_account for email in emails}
            | set(self.source_accounts)
            | set(self.unavailable)
        )
        return collect(
            month,
            sources=[
                InMemoryMailSource(
                    a,
                    [e for e in emails if e.source_account == a],
                    unavailable=self.unavailable.get(a),
                )
                for a in accounts
            ],
            pipeline=self._traced(
                Pipeline(
                    classifier=self.classifier or FakeClassifier(),
                    extractor=FakeExtractor.for_documents(self.answers),
                    renderer=self.renderer,
                    portal_fetcher=FakePortalFetcher(self.portal_pages),
                    exchange_rates=self.rates,
                    archive=self.archive or LocalArchive(self.tmp_path / "archive"),
                    ledger=self.ledger,
                    stronger_extractor=FakeExtractor.for_documents(self.stronger_answers),
                    vendor_matcher=self.vendor_matcher or RulesFirstVendorMatcher(),
                    clock=lambda: self.clock[0],
                )
            ),
            summary_writers=[],
            settings=Settings(
                search_window_days=window_days,
                anomaly_threshold=self.anomaly_threshold,
                retry_delays=self.retry_delays,
            ),
        )

    def _traced(self, pipeline: Pipeline) -> Pipeline:
        tracer = self.tracer
        if tracer is None:
            return pipeline
        stronger = pipeline.stronger_extractor
        return replace(
            pipeline,
            classifier=TracedClassifier(pipeline.classifier, tracer),
            extractor=TracedExtractor(pipeline.extractor, tracer, HAIKU),
            stronger_extractor=(
                TracedExtractor(stronger, tracer, SONNET) if stronger is not None else None
            ),
            vendor_matcher=TracedVendorMatcher(pipeline.vendor_matcher, tracer),
        )

    def expect(
        self,
        vendor: str,
        account: str | None = ENGINEERING,
        *,
        cycle: BillingCycle = "monthly",
        renewal_month: int | None = None,
        usual: str | None = None,
    ) -> None:
        amount = Decimal(usual) if usual is not None else None
        self.ledger.save_expected_vendor(
            ExpectedVendor(vendor, account, cycle, renewal_month, amount, "USD")
        )

    def saved_files(self, month: CollectionMonth = AUGUST) -> list[str]:
        folder = self.tmp_path / "archive" / str(month)
        return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


def invoice_email(
    vendor: str,
    pdf: bytes,
    *,
    received: datetime,
    account: str = ENGINEERING,
    subject: str | None = None,
) -> Email:
    return Email(
        source_account=account,
        message_id=f"m-{vendor.lower()}-{account.split('@')[0]}-{received:%m%d}",
        sender=f"{vendor} <billing@{vendor.lower()}.example>",
        subject=subject or f"Your {vendor} invoice",
        received_at=received,
        attachments=(Attachment("invoice.pdf", "application/pdf", pdf),),
    )


def notice(
    vendor: str, subject: str, body: str, *, received: datetime, account: str = ENGINEERING
) -> Email:
    """An email about billing that carries no billing document."""
    return Email(
        source_account=account,
        message_id=f"m-{vendor.lower()}-notice-{received:%m%d}",
        sender=f"{vendor} <billing@{vendor.lower()}.example>",
        subject=subject,
        received_at=received,
        text_body=body,
    )


def portal_email(
    vendor: str,
    url: str,
    *,
    received: datetime,
    account: str = ENGINEERING,
    sender: str | None = None,
    subject: str | None = None,
) -> Email:
    """An email whose billing document is behind a link to the vendor's billing page."""
    return Email(
        source_account=account,
        message_id=f"m-{vendor.lower().replace(' ', '-')}-portal-{received:%m%d}",
        sender=sender or f"{vendor} <billing@{vendor.lower().replace(' ', '')}.example>",
        subject=subject or f"Your {vendor} invoice is available",
        received_at=received,
        html_body=f'<p>Your latest invoice is ready.</p><a href="{url}">View invoice</a>',
    )


def usd(vendor: str, day: date, total: str, document_type: DocumentType = "invoice") -> Extraction:
    return Extraction(document_type, vendor, day, Decimal(total), "USD")


def real_pdf(title: str, password: str | None = None) -> bytes:
    """A PDF that opens, with one blank page and no text, so nothing reads fields from it.

    With a password, it opens only with that password.
    """
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_metadata({"/Title": title})
    if password is not None:
        writer.encrypt(password)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()
