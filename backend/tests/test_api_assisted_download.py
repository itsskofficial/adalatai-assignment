"""Assisted downloads, through the dashboard's API as the Review screen calls it.

An email is flagged by running a collection whose portal link leads to a sign-in page,
as a real run flags one. The PDF a person downloads is uploaded as the body of a request,
and read by a fake extractor.
"""

import io
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import anthropic
import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from support import AUGUST, ENGINEERING, OPS, Collection, invoice_email
from test_api import FINANCE, sign_in
from test_api_review import FakeDriveArchive

from invoice_collector.api import assisted_downloads
from invoice_collector.api.app import create_app
from invoice_collector.api.document_trail import document_trail
from invoice_collector.api.identity import FakeIdentityVerifier
from invoice_collector.api.serve import upload_extractors, upload_vendor_matcher
from invoice_collector.api.settings import Settings
from invoice_collector.archive import LocalArchive
from invoice_collector.classifier import FakeClassifier
from invoice_collector.claude_extractor import ClaudeExtractor
from invoice_collector.claude_vendor_matcher import ClaudeVendorMatcher
from invoice_collector.domain import CollectionMonth, Email, EmailState, Extraction
from invoice_collector.exchange_rates import FakeExchangeRates
from invoice_collector.extractor import (
    ExtractionFailed,
    FakeExtractor,
    FallbackExtractor,
    NotABillingDocument,
    content_hash,
)
from invoice_collector.ledger import Ledger
from invoice_collector.mail_source import InMemoryMailSource
from invoice_collector.portal import FakePortalFetcher, LoginGated
from invoice_collector.rule_extractor import RuleExtractor
from invoice_collector.run import Pipeline, RunResult, collect
from invoice_collector.run import Settings as RunSettings
from invoice_collector.vendor_matcher import (
    JevVendorMatcher,
    RulesFirstVendorMatcher,
    VendorMatch,
    VendorMatcher,
)

NOW = datetime(2026, 9, 29, 10, 30, tzinfo=UTC)
ZOOM_PORTAL = "https://zoom.example/billing/invoices/889"
REVIEW = "/api/months/2026-08/review"


def pdf_file(text: str, *, password: str | None = None) -> bytes:
    """A real one-page PDF, distinct for each text."""
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_metadata({"/Title": text})
    if password is not None:
        writer.encrypt(password)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


ZOOM_PDF = pdf_file("Zoom invoice 889")
ZOOM = Extraction("invoice", "Zoom", date(2026, 8, 12), Decimal("149.90"), "USD")


class CountingExtractor:
    """Reads prepared answers by the PDF's bytes, and counts how often it is asked."""

    def __init__(self, answers: Mapping[bytes, Extraction | Exception] | None = None) -> None:
        self.answers: dict[bytes, Extraction | Exception] = dict(answers or {})
        self.read: list[bytes] = []

    def extract(self, pdf: bytes) -> Extraction:
        self.read.append(pdf)
        answer = self.answers.get(pdf)
        if answer is None:
            raise ExtractionFailed("no prepared answer for this document")
        if isinstance(answer, Exception):
            raise answer
        return answer


def zoom_email(account: str = ENGINEERING, message_id: str = "m-zoom") -> Email:
    return Email(
        source_account=account,
        message_id=message_id,
        sender="Zoom <billing@zoom.example>",
        subject="Your Zoom invoice is ready",
        received_at=datetime(2026, 8, 12, 8, 0, tzinfo=UTC),
        html_body=f'<p>Your invoice is ready.</p><a href="{ZOOM_PORTAL}">View invoice</a>',
    )


def zoom_attached() -> Email:
    """The same Zoom invoice, attached to an email in another source account."""
    return invoice_email("Zoom", ZOOM_PDF, received=datetime(2026, 8, 12, tzinfo=UTC), account=OPS)


def run_month(
    collection: Collection, emails: list[Email], month: CollectionMonth = AUGUST
) -> RunResult:
    """A run whose portal links all lead to a sign-in page."""
    accounts = sorted({email.source_account for email in emails})
    return collect(
        month,
        sources=[
            InMemoryMailSource(a, [e for e in emails if e.source_account == a]) for a in accounts
        ],
        pipeline=Pipeline(
            classifier=FakeClassifier(),
            extractor=FakeExtractor.for_documents(collection.answers),
            renderer=collection.renderer,
            portal_fetcher=FakePortalFetcher({ZOOM_PORTAL: LoginGated()}),
            exchange_rates=collection.rates,
            archive=LocalArchive(collection.tmp_path / "archive"),
            ledger=collection.ledger,
        ),
        summary_writers=[],
        settings=RunSettings(),
    )


def flag_zoom(collection: Collection, *emails: Email) -> list[Email]:
    """Runs August, and the Zoom email's portal link is flagged for a manual download."""
    flagged = list(emails) or [zoom_email()]
    run_month(collection, flagged)
    for email in flagged:
        assert state_of(collection, email) == (EmailState.NEEDS_REVIEW, "manual download needed")
    return flagged


def state_of(collection: Collection, email: Email) -> tuple[EmailState, str | None]:
    for month in collection.ledger.months():
        for examined in collection.ledger.examined_emails(month):
            if (examined.source_account, examined.message_id) == (
                email.source_account,
                email.message_id,
            ):
                return examined.state, examined.reason
    raise AssertionError(f"{email.message_id} was not examined")


@pytest.fixture
def extractor() -> CountingExtractor:
    return CountingExtractor({ZOOM_PDF: ZOOM})


def open_dashboard(
    tmp_path: Path,
    extractor: CountingExtractor | None,
    *,
    stronger: CountingExtractor | None = None,
    drive: FakeDriveArchive | None = None,
    vendor_matcher: VendorMatcher | None = None,
) -> TestClient:
    ledger_path = tmp_path / "ledger.sqlite"
    settings = Settings(
        session_secret="a-secret-only-for-tests",
        allowlist=frozenset({FINANCE}),
        ledger_path=ledger_path,
    )
    app = create_app(
        settings,
        lambda: Ledger(ledger_path),
        FakeIdentityVerifier({"code-finance": FINANCE}),
        exchange_rates=FakeExchangeRates({"USD": Decimal("95.34")}),
        drive_archive=drive,
        now=lambda: NOW,
        extractor=extractor,
        stronger_extractor=stronger,
        vendor_matcher=vendor_matcher,
    )
    return TestClient(app, base_url="http://localhost:8000", follow_redirects=False)


@pytest.fixture
def dashboard(tmp_path: Path, extractor: CountingExtractor) -> Iterator[TestClient]:
    with open_dashboard(tmp_path, extractor) as client:
        sign_in(client)
        yield client


def upload(dashboard: TestClient, email: Email, pdf: bytes, month: str = "2026-08") -> Any:
    path = f"/api/months/{month}/review/{email.source_account}/{email.message_id}/upload"
    return dashboard.post(path, content=pdf, headers={"Content-Type": "application/pdf"})


def queue(dashboard: TestClient) -> list[dict[str, Any]]:
    return dashboard.get(REVIEW).json()["items"]


def summary(dashboard: TestClient) -> dict[str, Any]:
    return dashboard.get("/api/months/2026-08/summary").json()


# Filed and reported


def test_uploaded_pdf_is_extracted_filed_and_added_to_the_summary(
    collection: Collection, dashboard: TestClient, extractor: CountingExtractor
) -> None:
    [email] = flag_zoom(collection)

    response = upload(dashboard, email, ZOOM_PDF)

    assert response.status_code == 200
    answer = response.json()
    assert answer["outcome"] == "collected"
    assert answer["collection_month"] == "2026-08"
    assert answer["file_name"] == "2026-08_Zoom_149.90-USD.pdf"
    assert answer["document"]["vendor"] == "Zoom"
    assert answer["document"]["doubts"] == []
    assert extractor.read == [ZOOM_PDF]
    filed = collection.tmp_path / "archive" / "2026-08" / "2026-08_Zoom_149.90-USD.pdf"
    assert filed.read_bytes() == ZOOM_PDF
    [row] = summary(dashboard)["rows"]
    assert (row["vendor"], row["amount"], row["currency"]) == ("Zoom", "149.90", "USD")
    assert row["source_account"] == ENGINEERING
    assert row["amount_inr"] == "14291.47"
    assert dashboard.get(row["file_url"]).content == ZOOM_PDF


def test_upload_is_linked_to_the_email_that_carried_the_portal_link_and_clears_its_flag(
    collection: Collection, dashboard: TestClient
) -> None:
    [email] = flag_zoom(collection)
    assert [i["needs_manual_download"] for i in queue(dashboard)] == [True]

    upload(dashboard, email, ZOOM_PDF)

    assert state_of(collection, email) == (EmailState.COLLECTED, None)
    [examined] = collection.ledger.examined_emails(AUGUST)
    assert examined.portal_link == ZOOM_PORTAL
    [document] = collection.ledger.documents(AUGUST)
    source = collection.ledger.source_of(document.file_link)
    assert source is not None
    assert (source.source_account, source.message_id) == (ENGINEERING, email.message_id)
    assert queue(dashboard) == []
    month = summary(dashboard)
    assert month["needs_review"] == []
    assert month["counts"]["needs_review"] == 0
    assert month["counts"]["collected"] == 1


def test_upload_filed_by_a_person_survives_a_second_run_without_opening_the_link_again(
    collection: Collection, dashboard: TestClient
) -> None:
    [email] = flag_zoom(collection)
    upload(dashboard, email, ZOOM_PDF)

    result = run_month(collection, [email])

    assert [(row.vendor, row.total) for row in result.summary] == [("Zoom", Decimal("149.90"))]
    assert result.warnings == []
    assert state_of(collection, email) == (EmailState.COLLECTED, None)


def test_one_upload_settles_the_same_portal_link_in_every_source_account(
    collection: Collection, dashboard: TestClient
) -> None:
    first, second = flag_zoom(collection, zoom_email(), zoom_email(OPS, "m-zoom-ops"))

    response = upload(dashboard, second, ZOOM_PDF)

    assert response.status_code == 200
    assert state_of(collection, first) == (EmailState.COLLECTED, None)
    assert state_of(collection, second) == (EmailState.COLLECTED, None)
    [row] = summary(dashboard)["rows"]
    assert row["source_account"] == f"{ENGINEERING}; {OPS}"
    assert queue(dashboard) == []


def test_invoice_dated_in_another_month_is_filed_under_that_month(
    collection: Collection, dashboard: TestClient, extractor: CountingExtractor
) -> None:
    [email] = flag_zoom(collection)
    extractor.answers[ZOOM_PDF] = replace(ZOOM, invoice_date=date(2026, 7, 31))

    answer = upload(dashboard, email, ZOOM_PDF).json()

    assert answer["collection_month"] == "2026-07"
    assert (collection.tmp_path / "archive" / "2026-07" / answer["file_name"]).exists()
    july = dashboard.get("/api/months/2026-07/summary").json()
    assert [row["vendor"] for row in july["rows"]] == ["Zoom"]
    assert queue(dashboard) == []


# Held for review


def test_upload_that_fails_checks_is_held_for_review_like_any_other(
    collection: Collection, dashboard: TestClient, extractor: CountingExtractor
) -> None:
    [email] = flag_zoom(collection)
    extractor.answers[ZOOM_PDF] = replace(ZOOM, confidence="low", doubts="the date is smudged")

    answer = upload(dashboard, email, ZOOM_PDF).json()

    assert answer["outcome"] == "held"
    assert answer["document"]["doubts"] == [
        {"field": None, "reason": "the reader was unsure: the date is smudged"}
    ]
    assert state_of(collection, email) == (
        EmailState.NEEDS_REVIEW,
        "the reader was unsure: the date is smudged",
    )
    [item] = queue(dashboard)
    assert item["needs_manual_download"] is False
    assert item["portal_link"] == ZOOM_PORTAL
    [held] = item["documents"]
    assert held["vendor"] == "Zoom"
    assert held["doubts"] == answer["document"]["doubts"]
    assert dashboard.get(held["file_url"]).content == ZOOM_PDF
    assert summary(dashboard)["rows"] == []


def test_held_upload_is_approved_on_the_review_screen_and_reported(
    collection: Collection, dashboard: TestClient, extractor: CountingExtractor
) -> None:
    [email] = flag_zoom(collection)
    extractor.answers[ZOOM_PDF] = replace(ZOOM, confidence="low")
    upload(dashboard, email, ZOOM_PDF)
    [item] = queue(dashboard)
    [held] = item["documents"]
    fields = {k: held[k] for k in ("vendor", "invoice_date", "currency", "document_type")}

    response = dashboard.post(
        f"{REVIEW}/{ENGINEERING}/{email.message_id}/approve",
        json={"documents": [{"content_hash": held["content_hash"], **fields, "total": "149.00"}]},
    )

    assert response.status_code == 200
    [row] = summary(dashboard)["rows"]
    assert (row["vendor"], row["amount"]) == ("Zoom", "149.00")
    assert state_of(collection, email) == (EmailState.COLLECTED, None)


def test_held_upload_is_still_held_after_a_second_run(
    collection: Collection, dashboard: TestClient, extractor: CountingExtractor
) -> None:
    [email] = flag_zoom(collection)
    extractor.answers[ZOOM_PDF] = replace(ZOOM, confidence="low")
    upload(dashboard, email, ZOOM_PDF)

    result = run_month(collection, [email])

    assert [p.extraction.vendor for p in result.pending] == ["Zoom"]
    [item] = queue(dashboard)
    assert item["needs_manual_download"] is False


def test_total_far_from_the_vendors_usual_holds_the_upload(
    collection: Collection, dashboard: TestClient
) -> None:
    collection.expect("Zoom", usual="40.00")
    [email] = flag_zoom(collection)

    answer = upload(dashboard, email, ZOOM_PDF).json()

    assert answer["outcome"] == "held"
    [doubt] = answer["document"]["doubts"]
    assert doubt["field"] == "total"
    assert "above the usual 40.00 USD for Zoom" in doubt["reason"]


def test_doubted_reading_is_read_again_by_the_stronger_model(
    collection: Collection, tmp_path: Path
) -> None:
    [email] = flag_zoom(collection)
    first = CountingExtractor({ZOOM_PDF: replace(ZOOM, confidence="low")})
    stronger = CountingExtractor({ZOOM_PDF: ZOOM})
    with open_dashboard(tmp_path, first, stronger=stronger) as dashboard:
        sign_in(dashboard)

        answer = upload(dashboard, email, ZOOM_PDF).json()

    assert answer["outcome"] == "collected"
    assert stronger.read == [ZOOM_PDF]


# Known by what it was made from


def test_same_file_uploaded_twice_is_one_billing_document(
    collection: Collection, dashboard: TestClient, extractor: CountingExtractor
) -> None:
    [email] = flag_zoom(collection)

    first = upload(dashboard, email, ZOOM_PDF)
    second = upload(dashboard, email, ZOOM_PDF)

    assert (first.status_code, second.status_code) == (200, 200)
    assert second.json() == first.json()
    assert extractor.read == [ZOOM_PDF]
    assert collection.saved_files() == ["2026-08_Zoom_149.90-USD.pdf"]
    assert len(summary(dashboard)["rows"]) == 1
    assert len(dashboard.get(f"{REVIEW}/uploads").json()) == 1


def test_a_different_file_for_an_email_already_settled_is_refused(
    collection: Collection, dashboard: TestClient
) -> None:
    [email] = flag_zoom(collection)
    upload(dashboard, email, ZOOM_PDF)

    response = upload(dashboard, email, pdf_file("something else"))

    assert response.status_code == 404
    assert collection.saved_files() == ["2026-08_Zoom_149.90-USD.pdf"]


def test_document_already_collected_as_an_attachment_elsewhere_is_linked_not_filed_again(
    collection: Collection, dashboard: TestClient, extractor: CountingExtractor
) -> None:
    attached = zoom_attached()
    collection.answers[ZOOM_PDF] = ZOOM
    [email] = flag_zoom(collection)
    run_month(collection, [attached, email])

    answer = upload(dashboard, email, ZOOM_PDF).json()

    assert answer["outcome"] == "already_collected"
    assert extractor.read == []
    assert state_of(collection, email) == (EmailState.COLLECTED, None)
    [row] = summary(dashboard)["rows"]
    assert row["source_account"] == f"{ENGINEERING}; {OPS}"
    assert collection.saved_files() == ["2026-08_Zoom_149.90-USD.pdf"]


def test_same_file_as_a_document_held_for_review_is_refused_until_that_is_decided(
    collection: Collection, dashboard: TestClient
) -> None:
    attached = zoom_attached()
    collection.answers[ZOOM_PDF] = replace(ZOOM, confidence="low")
    [email] = flag_zoom(collection)
    run_month(collection, [attached, email])

    response = upload(dashboard, email, ZOOM_PDF)

    assert response.status_code == 409
    assert "already held for review" in response.json()["detail"]
    assert state_of(collection, email) == (EmailState.NEEDS_REVIEW, "manual download needed")


# The file is not trusted


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (b"", "No file was uploaded"),
        (b"<html><script>alert(1)</script></html>", "The file is not a PDF"),
        (b"MZ\x90\x00 an executable named invoice.pdf", "The file is not a PDF"),
        (b"%PDF-1.7 but nothing after it", "not a readable PDF"),
        (pdf_file("locked", password="secret"), "protected with a password"),
    ],
)
def test_a_file_that_is_not_a_usable_pdf_is_refused_by_its_content(
    collection: Collection,
    dashboard: TestClient,
    extractor: CountingExtractor,
    content: bytes,
    reason: str,
) -> None:
    [email] = flag_zoom(collection)

    response = upload(dashboard, email, content)

    assert response.status_code == 422
    assert reason in response.json()["detail"]
    assert extractor.read == []
    assert state_of(collection, email) == (EmailState.NEEDS_REVIEW, "manual download needed")
    assert collection.saved_files() == []


def test_a_file_over_the_size_limit_is_refused(
    collection: Collection,
    dashboard: TestClient,
    extractor: CountingExtractor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    [email] = flag_zoom(collection)
    monkeypatch.setattr(assisted_downloads, "MAX_UPLOAD_BYTES", len(ZOOM_PDF) - 1)

    response = upload(dashboard, email, ZOOM_PDF)

    assert response.status_code == 413
    assert extractor.read == []
    assert state_of(collection, email) == (EmailState.NEEDS_REVIEW, "manual download needed")


def test_a_pdf_that_is_not_a_billing_document_is_refused_and_the_email_stays_flagged(
    collection: Collection, dashboard: TestClient, extractor: CountingExtractor
) -> None:
    [email] = flag_zoom(collection)
    extractor.answers[ZOOM_PDF] = NotABillingDocument("it is a product brochure")

    response = upload(dashboard, email, ZOOM_PDF)

    assert response.status_code == 422
    assert "it is a product brochure" in response.json()["detail"]
    assert state_of(collection, email) == (EmailState.NEEDS_REVIEW, "manual download needed")
    assert dashboard.get(f"{REVIEW}/uploads").json() == []


def test_a_pdf_that_cannot_be_read_changes_nothing(
    collection: Collection, dashboard: TestClient, extractor: CountingExtractor
) -> None:
    [email] = flag_zoom(collection)
    extractor.answers[ZOOM_PDF] = ExtractionFailed("could not reach the model")

    response = upload(dashboard, email, ZOOM_PDF)

    assert response.status_code == 502
    assert "Nothing was changed" in response.json()["detail"]
    assert state_of(collection, email) == (EmailState.NEEDS_REVIEW, "manual download needed")
    assert collection.saved_files() == []


def test_without_an_extractor_uploading_is_unavailable(
    collection: Collection, tmp_path: Path
) -> None:
    [email] = flag_zoom(collection)
    with open_dashboard(tmp_path, None) as dashboard:
        sign_in(dashboard)

        response = upload(dashboard, email, ZOOM_PDF)

    assert response.status_code == 503
    assert state_of(collection, email) == (EmailState.NEEDS_REVIEW, "manual download needed")


# Which emails take an upload


def test_an_email_holding_a_document_to_review_takes_no_upload(
    collection: Collection, dashboard: TestClient
) -> None:
    held_pdf = b"%PDF-1.7 slack august"
    collection.answers[held_pdf] = Extraction(
        "invoice", "Slack", date(2026, 8, 3), Decimal("652.50"), "USD", confidence="low"
    )
    slack = invoice_email("Slack", held_pdf, received=datetime(2026, 8, 3, tzinfo=UTC))
    collection.run([slack])

    response = upload(dashboard, slack, ZOOM_PDF)

    assert response.status_code == 409
    assert "Review screen" in response.json()["detail"]


def test_an_email_that_is_not_waiting_is_not_found(dashboard: TestClient) -> None:
    response = upload(dashboard, zoom_email(), ZOOM_PDF)

    assert response.status_code == 404


# Who did what


def test_the_upload_is_recorded_with_who_made_it_and_when(
    collection: Collection, dashboard: TestClient
) -> None:
    [email] = flag_zoom(collection)
    upload(dashboard, email, ZOOM_PDF)

    [record] = dashboard.get(f"{REVIEW}/uploads").json()

    assert record["person"] == FINANCE
    assert record["uploaded_at"] == NOW.isoformat()
    assert record["outcome"] == "collected"
    assert (record["source_account"], record["message_id"]) == (ENGINEERING, email.message_id)
    assert record["subject"] == email.subject
    assert record["portal_link"] == ZOOM_PORTAL
    assert record["size"] == len(ZOOM_PDF)
    assert record["document"]["total"] == "149.90"


# Filing to the owner account's Drive


def test_upload_is_filed_to_the_owner_accounts_drive_as_a_run_files_one(
    collection: Collection, tmp_path: Path, extractor: CountingExtractor
) -> None:
    [email] = flag_zoom(collection)
    drive = FakeDriveArchive()
    with open_dashboard(tmp_path, extractor, drive=drive) as dashboard:
        sign_in(dashboard)

        response = upload(dashboard, email, ZOOM_PDF)

        assert response.status_code == 200
        name = "2026-08_Zoom_149.90-USD.pdf"
        assert drive.saved == [("2026-08", name, ZOOM_PDF)]
        assert collection.saved_files() == [name]
        [row] = summary(dashboard)["rows"]
        assert row["file_url"] == f"https://drive.example/2026-08/{name}"


def test_upload_while_drive_cannot_be_reached_changes_nothing(
    collection: Collection, tmp_path: Path, extractor: CountingExtractor
) -> None:
    [email] = flag_zoom(collection)
    drive = FakeDriveArchive(reachable=False)
    with open_dashboard(tmp_path, extractor, drive=drive) as dashboard:
        sign_in(dashboard)

        response = upload(dashboard, email, ZOOM_PDF)

        assert response.status_code == 502
        assert state_of(collection, email) == (EmailState.NEEDS_REVIEW, "manual download needed")
        assert dashboard.get(f"{REVIEW}/uploads").json() == []

        drive.reachable = True
        assert upload(dashboard, email, ZOOM_PDF).json()["outcome"] == "collected"


# Signing in


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", f"{REVIEW}/uploads"),
        ("POST", f"{REVIEW}/{ENGINEERING}/m-zoom/upload"),
    ],
)
def test_upload_routes_return_401_when_not_signed_in(
    tmp_path: Path, extractor: CountingExtractor, method: str, path: str
) -> None:
    with open_dashboard(tmp_path, extractor) as client:
        assert client.request(method, path, content=ZOOM_PDF).status_code == 401


def test_content_hash_of_an_upload_is_the_portal_links(
    collection: Collection, dashboard: TestClient
) -> None:
    """A later run knows the document by the link, as it knows any portal document."""
    [email] = flag_zoom(collection)
    upload(dashboard, email, ZOOM_PDF)

    [document] = collection.ledger.documents(AUGUST)

    assert document.content_hash == content_hash(ZOOM_PORTAL.encode())


# Matched to the expected vendor list, as a run matches a document


class KnowsAmazon:
    """Stands in for a model asked what rules cannot decide."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    def match(self, text: str, expected_vendors: Sequence[str]) -> VendorMatch:
        self.asked.append(text)
        return VendorMatch("AWS", 0.97, "jev-latest")


def test_upload_naming_the_vendor_another_way_takes_the_expected_spelling(
    collection: Collection, dashboard: TestClient, extractor: CountingExtractor
) -> None:
    collection.expect("Zoom")
    [email] = flag_zoom(collection)
    extractor.answers[ZOOM_PDF] = replace(ZOOM, vendor="Zoom Video Communications")

    answer = upload(dashboard, email, ZOOM_PDF).json()

    assert answer["document"]["vendor"] == "Zoom"
    assert answer["file_name"] == "2026-08_Zoom_149.90-USD.pdf"
    [document] = collection.ledger.documents(AUGUST)
    assert document.extraction.vendor == "Zoom"
    assert document.vendor_as_read == "Zoom Video Communications"
    [row] = summary(dashboard)["rows"]
    assert row["vendor"] == "Zoom"


def test_upload_is_matched_by_a_model_when_rules_cannot_decide(
    collection: Collection, tmp_path: Path, extractor: CountingExtractor
) -> None:
    collection.expect("AWS")
    [email] = flag_zoom(collection)
    extractor.answers[ZOOM_PDF] = replace(ZOOM, vendor="Amazon Web Services")
    model = KnowsAmazon()
    matcher = RulesFirstVendorMatcher(model)
    with open_dashboard(tmp_path, extractor, vendor_matcher=matcher) as dashboard:
        sign_in(dashboard)

        answer = upload(dashboard, email, ZOOM_PDF).json()

    assert answer["document"]["vendor"] == "AWS"
    assert model.asked and model.asked[0].startswith("Amazon Web Services\n")
    [document] = collection.ledger.documents(AUGUST)
    assert document.vendor_as_read == "Amazon Web Services"


def test_held_upload_keeps_the_name_as_read_beside_the_expected_spelling(
    collection: Collection, dashboard: TestClient, extractor: CountingExtractor
) -> None:
    collection.expect("Zoom")
    [email] = flag_zoom(collection)
    extractor.answers[ZOOM_PDF] = replace(
        ZOOM, vendor="Zoom Video Communications", confidence="low"
    )

    assert upload(dashboard, email, ZOOM_PDF).json()["outcome"] == "held"

    [held] = collection.ledger.pending(AUGUST)
    assert (held.extraction.vendor, held.vendor_as_read) == ("Zoom", "Zoom Video Communications")


def test_upload_from_a_vendor_on_no_list_keeps_its_name(
    collection: Collection, dashboard: TestClient
) -> None:
    collection.expect("Slack")
    [email] = flag_zoom(collection)

    answer = upload(dashboard, email, ZOOM_PDF).json()

    assert answer["document"]["vendor"] == "Zoom"
    [document] = collection.ledger.documents(AUGUST)
    assert document.vendor_as_read is None


# In the history of the billing document

ZOOM_HASH = content_hash(ZOOM_PORTAL.encode())


def upload_steps(collection: Collection) -> list[Any]:
    """The steps of the document's history taken at the moment of the upload."""
    history = document_trail(collection.tmp_path / "ledger.sqlite", ZOOM_HASH)
    assert history is not None
    return [entry for entry in history.entries if entry.at == NOW.isoformat()]


def test_upload_is_a_step_in_the_history_with_who_when_the_size_and_the_outcome(
    collection: Collection, dashboard: TestClient
) -> None:
    [email] = flag_zoom(collection)
    upload(dashboard, email, ZOOM_PDF)

    steps = upload_steps(collection)

    assert [step.kind for step in steps] == [
        "uploaded",
        "read",
        "checked",
        "checked",
        "filed",
        "converted",
        "collected",
    ]
    uploaded = steps[0]
    assert (uploaded.actor, uploaded.source_account) == (FINANCE, ENGINEERING)
    assert uploaded.details == {
        "size": len(ZOOM_PDF),
        "outcome": "collected",
        "file_name": "2026-08_Zoom_149.90-USD.pdf",
    }


def test_held_upload_shows_its_doubts_and_the_match_in_the_history(
    collection: Collection, dashboard: TestClient, extractor: CountingExtractor
) -> None:
    collection.expect("Zoom")
    [email] = flag_zoom(collection)
    extractor.answers[ZOOM_PDF] = replace(
        ZOOM, vendor="Zoom Video Communications", confidence="low", by="claude-haiku-4-5"
    )
    upload(dashboard, email, ZOOM_PDF)

    steps = {step.kind: step for step in upload_steps(collection)}

    assert steps["uploaded"].details["outcome"] == "held"
    assert steps["read"].actor == "claude-haiku-4-5"
    assert steps["matched"].actor == "rules"
    assert steps["matched"].details == {
        "as_read": "Zoom Video Communications",
        "expected_vendor": "Zoom",
    }
    assert steps["held"].details["doubts"][0]["reason"].startswith("the reader was unsure")


def test_one_upload_is_a_step_for_each_email_it_settles(
    collection: Collection, dashboard: TestClient
) -> None:
    first, second = flag_zoom(collection, zoom_email(), zoom_email(OPS, "m-zoom-ops"))
    upload(dashboard, second, ZOOM_PDF)

    uploaded = [step for step in upload_steps(collection) if step.kind == "uploaded"]

    assert {step.source_account for step in uploaded} == {
        first.source_account,
        second.source_account,
    }


def test_upload_linked_to_a_document_collected_before_is_a_step_of_that_document(
    collection: Collection, dashboard: TestClient
) -> None:
    attached = zoom_attached()
    collection.answers[ZOOM_PDF] = ZOOM
    [email] = flag_zoom(collection)
    run_month(collection, [attached, email])
    upload(dashboard, email, ZOOM_PDF)

    history = document_trail(collection.tmp_path / "ledger.sqlite", content_hash(ZOOM_PDF))

    assert history is not None
    [uploaded] = [entry for entry in history.entries if entry.kind == "uploaded"]
    assert uploaded.details["outcome"] == "already_collected"
    assert uploaded.actor == FINANCE


# Open questions the identity rules left


def test_invoice_uploaded_first_and_attached_later_is_one_charge(
    collection: Collection, dashboard: TestClient, extractor: CountingExtractor
) -> None:
    [email] = flag_zoom(collection)
    upload(dashboard, email, ZOOM_PDF)
    collection.answers[ZOOM_PDF] = ZOOM

    result = run_month(collection, [email, zoom_attached()])

    [row] = result.summary
    assert row.source_accounts == (ENGINEERING, OPS)
    assert collection.saved_files() == ["2026-08_Zoom_149.90-USD.pdf"]
    assert extractor.read == [ZOOM_PDF]
    assert state_of(collection, zoom_attached()) == (EmailState.COLLECTED, None)


def test_invoice_uploaded_and_held_then_attached_is_one_item_to_review(
    collection: Collection, dashboard: TestClient, extractor: CountingExtractor
) -> None:
    [email] = flag_zoom(collection)
    extractor.answers[ZOOM_PDF] = replace(ZOOM, confidence="low")
    upload(dashboard, email, ZOOM_PDF)

    result = run_month(collection, [email, zoom_attached()])

    assert {p.content_hash for p in result.pending} == {ZOOM_HASH}
    [item] = queue(dashboard)
    assert sorted(item["message_ids"]) == sorted([email.message_id, zoom_attached().message_id])


def test_held_upload_dated_in_another_month_stays_in_that_months_queue(
    collection: Collection, dashboard: TestClient, extractor: CountingExtractor
) -> None:
    [email] = flag_zoom(collection)
    extractor.answers[ZOOM_PDF] = replace(ZOOM, invoice_date=date(2026, 7, 30), confidence="low")
    assert upload(dashboard, email, ZOOM_PDF).json()["collection_month"] == "2026-07"

    result = run_month(collection, [email])

    assert result.pending == []
    assert queue(dashboard) == []
    [held] = dashboard.get("/api/months/2026-07/review").json()["items"]
    fields = {
        "document_type": "invoice",
        "vendor": "Zoom",
        "invoice_date": "2026-07-30",
        "total": "149.90",
        "currency": "USD",
    }
    [document] = held["documents"]
    approval = {"documents": [{"content_hash": document["content_hash"], **fields}]}
    path = f"/api/months/2026-07/review/{ENGINEERING}/{email.message_id}/approve"
    assert dashboard.post(path, json=approval).status_code == 200
    july = dashboard.get("/api/months/2026-07/summary").json()
    assert [row["vendor"] for row in july["rows"]] == ["Zoom"]


# The dashboard command


def test_dashboard_command_reads_uploads_with_rules_when_no_model_can_be_used() -> None:
    extractor, stronger = upload_extractors(None, {})

    assert isinstance(extractor, RuleExtractor)
    assert stronger is None


def test_dashboard_command_reads_uploads_with_the_models_a_collection_uses() -> None:
    claude = anthropic.Anthropic(api_key="not-a-real-key", base_url="http://127.0.0.1:9")

    extractor, stronger = upload_extractors(claude, {})

    assert isinstance(extractor, FallbackExtractor)
    assert isinstance(stronger, ClaudeExtractor)


def test_dashboard_command_matches_uploads_by_rules_when_no_model_can_be_used() -> None:
    matcher = upload_vendor_matcher(None, {})

    assert isinstance(matcher, RulesFirstVendorMatcher)
    assert matcher.models == ()


def test_dashboard_command_matches_uploads_with_the_models_a_collection_uses() -> None:
    claude = anthropic.Anthropic(api_key="not-a-real-key", base_url="http://127.0.0.1:9")

    matcher = upload_vendor_matcher(claude, {"JEV_API_KEY": "not-a-real-key"})

    assert isinstance(matcher, RulesFirstVendorMatcher)
    assert [type(model) for model in matcher.models] == [JevVendorMatcher, ClaudeVendorMatcher]
