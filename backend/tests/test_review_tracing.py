"""A decision on the Review screen, recorded as scores on the traces of the model calls it
judged. The run that held the document traced its calls to a fake tracer. See ADR 0017.
"""

import json
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from support import HAIKU, Collection, TracedExtractor, usd
from test_api import FINANCE, sign_in
from test_api_assisted_download import (
    ZOOM,
    ZOOM_PDF,
    ZOOM_PORTAL,
    CountingExtractor,
    flag_zoom,
    upload,
)
from test_api_review import NOW, action_path, approve, hold_slack

from invoice_collector import trail
from invoice_collector.api.app import create_app
from invoice_collector.api.document_trail import document_trail
from invoice_collector.api.identity import FakeIdentityVerifier
from invoice_collector.api.review_scores import Reviewed, scores_of
from invoice_collector.api.settings import Settings
from invoice_collector.exchange_rates import FakeExchangeRates
from invoice_collector.extractor import content_hash
from invoice_collector.ledger import Ledger
from invoice_collector.tracing import FakeTracer, Score


def open_dashboard(tmp_path: Path, tracer: FakeTracer) -> TestClient:
    ledger_path = tmp_path / "ledger.sqlite"
    app = create_app(
        Settings(
            session_secret="a-secret-only-for-tests",
            allowlist=frozenset({FINANCE}),
            ledger_path=ledger_path,
        ),
        lambda: Ledger(ledger_path),
        FakeIdentityVerifier({"code-finance": FINANCE}),
        exchange_rates=FakeExchangeRates({"USD": Decimal("95.34")}),
        now=lambda: NOW,
        tracer=tracer,
    )
    return TestClient(app, base_url="http://localhost:8000", follow_redirects=False)


@pytest.fixture
def tracer() -> FakeTracer:
    return FakeTracer()


@pytest.fixture
def dashboard(tmp_path: Path, tracer: FakeTracer) -> Iterator[TestClient]:
    with open_dashboard(tmp_path, tracer) as client:
        sign_in(client)
        yield client


def trace_of(tracer: FakeTracer, step: str) -> str:
    [call] = [c for c in tracer.calls if c.step == step]
    return call.link.trace_id


def test_a_correction_is_scored_on_the_trace_of_the_reading(
    collection: Collection, dashboard: TestClient, tracer: FakeTracer
) -> None:
    collection.tracer = tracer
    email = hold_slack(collection)

    response = approve(dashboard, email, total="625.50")

    assert response.status_code == 200
    reading = tracer.scores[trace_of(tracer, "extraction")]
    by_name = {score.name: score for score in reading}
    assert (by_name["review.total"].value, by_name["review.total"].type) == (0, "boolean")
    assert by_name["review.total"].comment == "corrected in review to 625.50"
    assert by_name["review.vendor"].value == 1
    assert by_name["review.invoice_date"].value == 1
    correction = by_name["output"]
    assert correction.type == "correction"
    assert json.loads(str(correction.value))["total"] == "625.50"
    # The person who decided is not sent.
    assert all(FINANCE not in (score.comment or "") for score in reading)
    assert trace_of(tracer, "classification") not in tracer.scores


def test_a_confirmation_without_changes_scores_every_field_right(
    collection: Collection, dashboard: TestClient, tracer: FakeTracer
) -> None:
    collection.tracer = tracer
    email = hold_slack(collection)

    approve(dashboard, email)

    reading = tracer.scores[trace_of(tracer, "extraction")]
    assert {score.name: score.value for score in reading} == {
        "review.vendor": 1,
        "review.invoice_date": 1,
        "review.total": 1,
        "review.currency": 1,
        "review.document_type": 1,
    }


def test_judging_an_email_not_billing_scores_its_classification_and_reading(
    collection: Collection, dashboard: TestClient, tracer: FakeTracer
) -> None:
    collection.tracer = tracer
    email = hold_slack(collection)

    response = dashboard.post(action_path(email, "reject"))

    assert response.status_code == 200
    judged = Score(
        "review.billing_document", 0, "boolean", "a person judged it not to be a billing document"
    )
    assert tracer.scores[trace_of(tracer, "classification")] == [judged]
    assert tracer.scores[trace_of(tracer, "extraction")] == [judged]


def test_a_tracer_that_fails_does_not_stop_a_decision(
    collection: Collection, tmp_path: Path
) -> None:
    collection.tracer = FakeTracer()
    email = hold_slack(collection)

    with open_dashboard(tmp_path, FakeTracer(failing=True)) as client:
        sign_in(client)
        response = approve(client, email, total="625.50")

    assert response.status_code == 200
    assert collection.saved_files() == ["2026-08_Slack_625.50-USD.pdf"]


def test_a_document_read_without_tracing_is_not_scored(
    collection: Collection, dashboard: TestClient, tracer: FakeTracer
) -> None:
    email = hold_slack(collection)

    approve(dashboard, email, total="625.50")

    assert tracer.scores == {}


def test_a_vendor_is_scored_on_the_match_that_chose_its_spelling() -> None:
    read = usd("Amazon Web Services", date(2026, 8, 3), "90.00")
    confirmed = usd("AWS Marketplace", date(2026, 8, 3), "90.00")
    traces = {trail.READ: "r" * 32, trail.MATCHED: "m" * 32, trail.CLASSIFIED: "c" * 32}

    scored = scores_of(Reviewed("h" * 64, read, confirmed), traces)

    assert [s.name for s in scored["m" * 32]] == ["review.vendor"]
    assert scored["m" * 32][0].value == 0
    assert "review.vendor" not in {s.name for s in scored["r" * 32]}
    assert "c" * 32 not in scored


def test_nothing_is_scored_without_a_trace() -> None:
    read = usd("Slack", date(2026, 8, 3), "652.50")

    assert scores_of(Reviewed("h" * 64, read, read), {}) == {}
    assert scores_of(Reviewed("h" * 64, read, None), {}) == {}


def test_an_upload_read_by_a_model_is_traced_and_linked_from_its_history(
    collection: Collection, tmp_path: Path
) -> None:
    tracer = FakeTracer()
    [email] = flag_zoom(collection)
    ledger_path = tmp_path / "ledger.sqlite"
    app = create_app(
        Settings(
            session_secret="a-secret-only-for-tests",
            allowlist=frozenset({FINANCE}),
            ledger_path=ledger_path,
        ),
        lambda: Ledger(ledger_path),
        FakeIdentityVerifier({"code-finance": FINANCE}),
        exchange_rates=FakeExchangeRates({"USD": Decimal("95.34")}),
        now=lambda: NOW,
        extractor=TracedExtractor(CountingExtractor({ZOOM_PDF: ZOOM}), tracer, HAIKU),
    )

    with TestClient(app, base_url="http://localhost:8000", follow_redirects=False) as client:
        sign_in(client)
        response = upload(client, email, ZOOM_PDF)

    assert response.status_code == 200
    [call] = tracer.calls
    assert call.step == "extraction"
    assert call.context.collection_month == "2026-08"
    assert (call.context.source_account, call.context.message_id) == (
        email.source_account,
        email.message_id,
    )
    assert call.context.invoice_format == "portal_link"
    history = document_trail(ledger_path, content_hash(ZOOM_PORTAL.encode()))
    assert history is not None
    [read] = [e for e in history.entries if e.kind == "read"]
    assert read.details["trace_id"] == call.link.trace_id
