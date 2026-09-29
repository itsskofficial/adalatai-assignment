"""Tests at the run seam: the history of each billing document, read after a run."""

import sqlite3
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from support import (
    AUGUST,
    ENGINEERING,
    OPS,
    RUN_AT,
    Collection,
    invoice_email,
    real_pdf,
    usd,
)
from test_run_checks import SLACK_PDF, august, slack_email

from invoice_collector import trail
from invoice_collector.api.document_trail import DocumentTrail, document_trail
from invoice_collector.archive import LocalArchive
from invoice_collector.classifier import FakeClassifier, FallbackClassifier
from invoice_collector.domain import Classification, Email, EmailState
from invoice_collector.exchange_rates import FakeExchangeRates
from invoice_collector.extractor import FakeExtractor, content_hash
from invoice_collector.ledger import CollectedDocument, Ledger
from invoice_collector.mail_source import InMemoryMailSource
from invoice_collector.portal import FakePortalFetcher
from invoice_collector.run import Pipeline, collect
from invoice_collector.vendor_matcher import RulesFirstVendorMatcher, VendorMatch

HAIKU = "claude-haiku-4-5"
SONNET = "claude-sonnet-5-5"
SLACK = replace(usd("Slack", date(2026, 8, 3), "652.50"), by=HAIKU)
SLACK_HASH = content_hash(SLACK_PDF)


def trail_of(collection: Collection, digest: str = SLACK_HASH) -> DocumentTrail:
    found = document_trail(collection.tmp_path / "ledger.sqlite", digest)
    assert found is not None
    return found


def kinds(history: DocumentTrail) -> list[str]:
    return [entry.kind for entry in history.entries]


def entry(history: DocumentTrail, kind: str, number: int = 0) -> Any:
    return [e for e in history.entries if e.kind == kind][number]


# A document collected


def test_trail_of_a_collected_document_runs_from_the_email_to_the_rupee_rate(
    collection: Collection,
) -> None:
    collection.answers[SLACK_PDF] = SLACK
    collection.expect("Slack", usual="640.00")

    collection.run([slack_email()])

    history = trail_of(collection)
    assert kinds(history) == [
        "received",
        "classified",
        "found",
        "read",
        "checked",
        "checked",
        "filed",
        "converted",
        "collected",
    ]
    assert history.state == "collected"
    assert history.invoice_format == "attachment"
    assert history.source_accounts == [ENGINEERING]
    assert history.file_name == "2026-08_Slack_652.50-USD.pdf"
    assert history.recorded_before_trail is False


def test_trail_shows_the_source_email_and_each_account_it_arrived_in(
    collection: Collection,
) -> None:
    collection.answers[SLACK_PDF] = SLACK
    first = slack_email()
    second = invoice_email("Slack", SLACK_PDF, received=august(4), account=OPS)

    collection.run([first, second])

    history = trail_of(collection)
    received = [e for e in history.entries if e.kind == "received"]
    assert [(e.source_account, e.at) for e in received] == [
        (ENGINEERING, first.received_at.isoformat()),
        (OPS, second.received_at.isoformat()),
    ]
    assert received[0].details == {
        "sender": first.sender,
        "subject": first.subject,
        "message_id": first.message_id,
    }
    assert history.source_accounts == [ENGINEERING, OPS]
    # The document was read once, from the first account; the second found it known.
    assert kinds(history).count("read") == 1
    assert kinds(history).count("collected") == 2


def test_trail_names_the_classifier_and_what_it_judged(collection: Collection) -> None:
    collection.answers[SLACK_PDF] = SLACK

    collection.run([slack_email()])

    classified = entry(trail_of(collection), "classified")
    assert classified.actor == "rules"
    assert classified.details == {
        "kind": "invoice",
        "vendor": "Slack",
        "confidence": "low",
        "probability": None,
    }


def test_trail_names_the_model_that_read_it_and_what_it_read(collection: Collection) -> None:
    collection.answers[SLACK_PDF] = SLACK

    collection.run([slack_email()])

    read = entry(trail_of(collection), "read")
    assert read.actor == HAIKU
    assert read.details == {
        "fields": {
            "vendor": "Slack",
            "invoice_date": "2026-08-03",
            "total": "652.50",
            "currency": "USD",
            "document_type": "invoice",
        },
        "confidence": "high",
        "reader_note": "",
    }


def test_trail_lists_each_check_that_ran_and_its_result(collection: Collection) -> None:
    collection.answers[SLACK_PDF] = SLACK
    collection.expect("Slack", usual="640.00")

    collection.run([slack_email(text_body="Total due: $652.50")])

    history = trail_of(collection)
    reading, against_history = (
        entry(history, "checked", 0).details,
        entry(history, "checked", 1).details,
    )
    assert reading["stage"] == "reading"
    assert [(c["check"], c["passed"]) for c in reading["checks"]] == [
        ("the reader's own confidence", True),
        ("the total the email states", True),
    ]
    assert against_history["stage"] == "history"
    assert [(c["check"], c["passed"]) for c in against_history["checks"]] == [
        ("the vendor's usual currency", True),
        ("the vendor's usual amount", True),
        ("one invoice from the vendor this month", True),
    ]


def test_trail_shows_where_it_was_filed_and_the_rate_and_its_date(
    collection: Collection,
) -> None:
    collection.answers[SLACK_PDF] = SLACK

    collection.run([slack_email()])

    history = trail_of(collection)
    assert entry(history, "filed").details == {
        "file_name": "2026-08_Slack_652.50-USD.pdf",
        "pending": False,
        "current": True,
        "web_link": None,
    }
    assert entry(history, "converted").details == {
        "currency": "USD",
        "rate": "95.34",
        "rate_date": "2026-08-03",
    }
    assert entry(history, "filed").at == RUN_AT.isoformat()


# Doubts, reading again and holding


def test_trail_shows_each_doubt_and_that_the_document_was_held(collection: Collection) -> None:
    collection.answers[SLACK_PDF] = replace(SLACK, confidence="low", doubts="a smudge")

    collection.run([slack_email()])

    history = trail_of(collection)
    assert history.state == "needs_review"
    [check] = entry(history, "checked").details["checks"]
    assert check == {
        "check": "the reader's own confidence",
        "passed": False,
        "doubts": [{"field": None, "reason": "the reader was unsure: a smudge"}],
    }
    assert entry(history, "held").details == {
        "doubts": [{"field": None, "reason": "the reader was unsure: a smudge"}],
        "waits_with_email": False,
    }
    assert entry(history, "filed").details["pending"] is True
    assert history.file_url == (
        "/api/months/2026-08/review/billing-documents/2026-08_Slack_652.50-USD.pdf"
    )


def test_trail_shows_the_stronger_model_reading_again_and_what_changed(
    collection: Collection,
) -> None:
    collection.answers[SLACK_PDF] = replace(SLACK, total=Decimal("625.50"))
    collection.stronger_answers[SLACK_PDF] = replace(SLACK, by=SONNET)

    collection.run([slack_email(text_body="Total due: $652.50")])

    history = trail_of(collection)
    assert kinds(history)[3:6] == ["read", "checked", "read_again"]
    read_again = entry(history, "read_again")
    assert read_again.actor == SONNET
    assert read_again.details["changes"] == [
        {"field": "total", "before": "625.50", "after": "652.50"}
    ]
    first, second = entry(history, "checked", 0), entry(history, "checked", 1)
    assert [c["passed"] for c in first.details["checks"]] == [True, False]
    assert [c["passed"] for c in second.details["checks"]] == [True, True]
    assert history.state == "collected"


def test_trail_records_when_the_stronger_model_could_not_read_it(
    collection: Collection,
) -> None:
    collection.answers[SLACK_PDF] = replace(SLACK, confidence="low")

    collection.run([slack_email()])

    failed = entry(trail_of(collection), "read_again_failed")
    assert failed.details == {"reason": "no prepared answer for this document"}


def test_document_that_waits_with_a_doubted_one_says_so(collection: Collection) -> None:
    notion_pdf = b"%PDF-1.7 notion august"
    collection.answers[SLACK_PDF] = replace(SLACK, confidence="low")
    collection.answers[notion_pdf] = replace(usd("Notion", date(2026, 8, 3), "96.00"), by=HAIKU)
    email = slack_email()
    email = replace(
        email,
        attachments=(*email.attachments, replace(email.attachments[0], content=notion_pdf)),
    )

    collection.run([email])

    held = entry(trail_of(collection, content_hash(notion_pdf)), "held")
    assert held.details == {"doubts": [], "waits_with_email": True}


# What is never shown


def test_trail_keeps_only_the_host_of_a_portal_link(tmp_path: Path) -> None:
    url = "https://billing.zoom.example/invoices/889?token=secret-token-1234"
    pdf = b"%PDF-1.7 zoom august"
    email = Email(
        source_account=ENGINEERING,
        message_id="m-zoom",
        sender="Zoom <billing@zoom.example>",
        subject="Your Zoom invoice",
        received_at=august(5),
        text_body=f"View your invoice: {url}",
    )
    ledger = Ledger(tmp_path / "ledger.sqlite")
    collect(
        AUGUST,
        sources=[InMemoryMailSource(ENGINEERING, [email])],
        pipeline=Pipeline(
            classifier=FakeClassifier(),
            extractor=FakeExtractor.for_documents(
                {pdf: replace(usd("Zoom", date(2026, 8, 5), "15.99"), by=HAIKU)}
            ),
            renderer=_NoRenderer(),
            portal_fetcher=FakePortalFetcher({url: pdf}),
            exchange_rates=FakeExchangeRates({"USD": Decimal("95.34")}),
            archive=LocalArchive(tmp_path / "archive"),
            ledger=ledger,
            clock=lambda: RUN_AT,
        ),
        summary_writers=[],
    )
    ledger.close()

    history = document_trail(tmp_path / "ledger.sqlite", content_hash(url.encode()))

    assert history is not None
    assert history.invoice_format == "portal_link"
    found = [e for e in history.entries if e.kind == "found"][0]
    assert found.details == {"invoice_format": "portal_link", "portal_host": "billing.zoom.example"}
    assert "secret-token-1234" not in history.model_dump_json()


def test_trail_never_holds_the_body_of_an_email(collection: Collection) -> None:
    body = "<p>Invoice for August. Total due: $652.50. Private note: the door code is 4411.</p>"
    rendered = f"%PDF-rendered 1 {body}".encode()
    collection.answers[rendered] = SLACK
    email = replace(slack_email(), attachments=(), html_body=body)

    collection.run([email])

    history = trail_of(collection, content_hash(" ".join(body.split()).encode()))
    assert history.invoice_format == "body"
    assert entry(history, "found").details == {"invoice_format": "body"}
    assert "door code" not in history.model_dump_json()


# Runs again, ledgers from before, and kinds added later


def test_running_the_month_again_does_not_lengthen_the_trail(collection: Collection) -> None:
    collection.answers[SLACK_PDF] = SLACK
    collection.run([slack_email()])
    before = trail_of(collection)

    collection.clock[0] = datetime(2026, 9, 4, 6, 0, tzinfo=UTC)
    collection.run([slack_email()])

    assert trail_of(collection) == before


def failing_after_a_second_reading(collection: Collection) -> Email:
    """An email that fails on every run, after its Slack invoice was doubted and read again.

    Its second attachment opens but nothing can read it, so the email fails and is examined
    afresh by every run, and the steps of the Slack invoice are recorded each time.
    """
    collection.answers[SLACK_PDF] = replace(SLACK, total=Decimal("625.50"))
    collection.stronger_answers[SLACK_PDF] = replace(SLACK, by=SONNET)
    email = slack_email(text_body="Total due: $652.50")
    unreadable = replace(email.attachments[0], filename="terms.pdf", content=real_pdf("terms"))
    return replace(email, attachments=(*email.attachments, unreadable))


def test_email_examined_again_keeps_the_same_trail_when_nothing_changed(
    collection: Collection,
) -> None:
    email = failing_after_a_second_reading(collection)
    collection.run([email])
    first = trail_of(collection)
    assert [e.details["stage"] for e in first.entries if e.kind == "checked"] == [
        "reading",
        "reading",
    ]

    for day in (4, 5):
        collection.clock[0] = datetime(2026, 9, day, 6, 0, tzinfo=UTC)
        collection.run([email])

        assert trail_of(collection) == first


def test_document_read_again_with_a_different_result_records_the_new_reading(
    collection: Collection,
) -> None:
    email = failing_after_a_second_reading(collection)
    collection.run([email])
    before = kinds(trail_of(collection))

    collection.clock[0] = datetime(2026, 9, 4, 6, 0, tzinfo=UTC)
    collection.answers[SLACK_PDF] = replace(SLACK, total=Decimal("625.00"))
    collection.run([email])

    history = trail_of(collection)
    added = [e for e in history.entries if e.at == collection.clock[0].isoformat()]
    assert [e.kind for e in added] == ["read", "checked", "read_again", "checked"]
    assert entry(history, "read", 1).details["fields"]["total"] == "625.00"
    assert kinds(history)[: len(before)] == before


def test_document_collected_before_the_trail_was_kept_shows_a_shorter_trail(
    collection: Collection,
) -> None:
    email = slack_email()
    # Recorded as an earlier version recorded it: the outcome, with no events.
    collection.ledger.record(
        AUGUST,
        email,
        EmailState.COLLECTED,
        documents=(
            CollectedDocument(
                SLACK_HASH,
                SLACK,
                "archive/2026-08/2026-08_Slack_652.50-USD.pdf",
                Decimal("95.34"),
            ),
        ),
    )

    history = trail_of(collection)

    assert kinds(history) == ["received", "filed", "converted"]
    assert [e.at for e in history.entries][1:] == [None, None]
    assert history.recorded_before_trail is True
    assert history.state == "collected"
    assert history.fields is not None and history.fields["total"] == "652.50"


def test_ledger_with_no_events_table_gives_a_trail(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite"
    Ledger(path).close()
    db = sqlite3.connect(path)
    db.execute("DROP TABLE document_events")
    db.execute(
        "INSERT INTO emails (source_account, message_id, collection_month, sender, subject, "
        "received_at, state) VALUES (?, 'm-1', '2026-08', 'Slack <b@slack.example>', "
        "'Your invoice', '2026-08-03T09:00:00+00:00', 'collected')",
        (ENGINEERING,),
    )
    db.execute(
        "INSERT INTO billing_documents (source_account, message_id, content_hash, file_link, "
        "document_type, vendor, invoice_date, total, currency) VALUES "
        "(?, 'm-1', ?, 'archive/2026-08/x.pdf', 'invoice', 'Slack', '2026-08-03', '652.50', "
        "'USD')",
        (ENGINEERING, SLACK_HASH),
    )
    db.commit()
    db.close()

    history = document_trail(path, SLACK_HASH)

    assert history is not None
    assert kinds(history) == ["received", "filed", "converted"]
    assert entry(history, "converted").details["rate"] is None


def test_unknown_document_has_no_trail(collection: Collection) -> None:
    assert document_trail(collection.tmp_path / "ledger.sqlite", "0" * 64) is None


def test_trail_keeps_event_kinds_it_does_not_name(collection: Collection) -> None:
    collection.answers[SLACK_PDF] = SLACK
    collection.run([slack_email()])

    collection.ledger.record_events(
        [
            trail.Event(
                kind="matched_to_vendor",
                source_account=ENGINEERING,
                message_id=slack_email().message_id,
                happened_at=datetime(2026, 9, 5, tzinfo=UTC),
                content_hash=SLACK_HASH,
                actor="rules",
                details={"stated_vendor": "Slack Technologies", "expected_vendor": "Slack"},
            )
        ]
    )

    added = trail_of(collection).entries[-1]
    assert (added.kind, added.actor) == ("matched_to_vendor", "rules")
    assert added.details == {"stated_vendor": "Slack Technologies", "expected_vendor": "Slack"}


def test_classification_by_a_model_is_named(collection: Collection, tmp_path: Path) -> None:
    email = slack_email()
    ledger = collection.ledger
    collect(
        AUGUST,
        sources=[InMemoryMailSource(ENGINEERING, [email])],
        pipeline=Pipeline(
            classifier=FakeClassifier(
                {email.message_id: Classification("invoice", "Slack", "high", 0.97, "jev-latest")}
            ),
            extractor=FakeExtractor.for_documents({SLACK_PDF: SLACK}),
            renderer=_NoRenderer(),
            portal_fetcher=FakePortalFetcher({}),
            exchange_rates=FakeExchangeRates({"USD": Decimal("95.34")}),
            archive=LocalArchive(tmp_path / "archive"),
            ledger=ledger,
        ),
        summary_writers=[],
    )

    classified = entry(trail_of(collection), "classified")
    assert classified.actor == "jev-latest"
    assert classified.details["probability"] == 0.97


def test_classifier_that_answered_is_named_when_one_in_doubt_was_followed(
    collection: Collection,
) -> None:
    email = slack_email()
    collection.answers[SLACK_PDF] = SLACK
    collection.classifier = FallbackClassifier(
        FakeClassifier(
            {email.message_id: Classification("not_billing", None, "low", 0.41, "jev-latest")}
        ),
        FakeClassifier({email.message_id: Classification("invoice", "Slack", "high", by=HAIKU)}),
    )

    collection.run([email])

    classified = entry(trail_of(collection), "classified")
    assert classified.actor == HAIKU
    assert classified.details["kind"] == "invoice"


class _NoRenderer:
    def render_html(self, html: str) -> bytes:
        raise AssertionError("nothing is rendered in this test")


# What the run gained: vendor matching, retries and PDFs that cannot be opened


class _Matcher:
    """Stands in for a model asked what rules cannot decide."""

    def __init__(self, answer: str, by: str, *, raises_first: int = 0) -> None:
        self._answer = answer
        self._by = by
        self._raises = raises_first

    def match(self, text: str, expected_vendors: Sequence[str]) -> VendorMatch:
        if self._raises:
            self._raises -= 1
            raise ConnectionResetError("the connection was reset")
        return VendorMatch(self._answer, 0.97, self._by)


AWS_PDF = b"%PDF-1.7 amazon web services august"
AMAZON = usd("Amazon Web Services", date(2026, 8, 2), "2691.60")
AWS_HASH = content_hash(AWS_PDF)


def aws_email(collection: Collection) -> Email:
    collection.answers[AWS_PDF] = AMAZON
    collection.expect("AWS")
    return invoice_email("Amazon", AWS_PDF, received=datetime(2026, 8, 2, 9, 0, tzinfo=UTC))


def test_trail_shows_the_vendor_matched_by_a_model_and_the_name_as_read(
    collection: Collection,
) -> None:
    email = aws_email(collection)
    collection.vendor_matcher = RulesFirstVendorMatcher(_Matcher("AWS", "jev-latest"))

    collection.run([email])

    history = trail_of(collection, AWS_HASH)
    assert kinds(history) == [
        "received",
        "classified",
        "found",
        "read",
        "checked",
        "matched",
        "checked",
        "filed",
        "converted",
        "collected",
    ]
    matched = entry(history, "matched")
    assert matched.actor == "jev-latest"
    assert matched.details == {"as_read": "Amazon Web Services", "expected_vendor": "AWS"}
    # What was read is kept as it was read.
    assert entry(history, "read").details["fields"]["vendor"] == "Amazon Web Services"


def test_trail_shows_a_legal_name_matched_by_rules(collection: Collection) -> None:
    collection.answers[SLACK_PDF] = replace(SLACK, vendor="Slack, Inc.")
    collection.expect("Slack")

    collection.run([slack_email()])

    matched = entry(trail_of(collection), "matched")
    assert matched.actor == "rules"
    assert matched.details == {"as_read": "Slack, Inc.", "expected_vendor": "Slack"}


def test_name_that_needs_no_match_adds_no_step(collection: Collection) -> None:
    collection.answers[SLACK_PDF] = SLACK
    collection.expect("Slack")

    collection.run([slack_email()])

    assert "matched" not in kinds(trail_of(collection))


def test_rerun_that_matches_a_document_collected_before_records_the_match(
    collection: Collection,
) -> None:
    email = aws_email(collection)
    collection.run([email])
    assert "matched" not in kinds(trail_of(collection, AWS_HASH))
    collection.vendor_matcher = RulesFirstVendorMatcher(_Matcher("AWS", "claude-haiku-4-5"))

    collection.run([email])

    matched = entry(trail_of(collection, AWS_HASH), "matched")
    assert matched.actor == "claude-haiku-4-5"
    assert matched.details == {"as_read": "Amazon Web Services", "expected_vendor": "AWS"}


def test_trail_shows_each_retry_and_only_the_steps_of_the_attempt_that_completed(
    collection: Collection,
) -> None:
    email = aws_email(collection)
    collection.vendor_matcher = RulesFirstVendorMatcher(
        _Matcher("AWS", "jev-latest", raises_first=2)
    )

    collection.run([email])

    history = trail_of(collection, AWS_HASH)
    assert kinds(history)[:4] == ["received", "retried", "retried", "classified"]
    assert kinds(history).count("read") == 1
    first, second = [e for e in history.entries if e.kind == "retried"]
    reason = "could not be examined: ConnectionResetError: the connection was reset"
    assert first.details == {"attempt": 2, "after": reason}
    assert second.details == {"attempt": 3, "after": reason}
    assert history.state == "collected"


def test_email_that_needed_no_retry_has_no_retry_step(collection: Collection) -> None:
    collection.answers[SLACK_PDF] = SLACK

    collection.run([slack_email()])

    assert "retried" not in kinds(trail_of(collection))


def test_trail_shows_a_pdf_that_could_not_be_opened_held_as_it_is(
    collection: Collection,
) -> None:
    locked = real_pdf("slack august", password="s3cret")
    email = invoice_email("Slack", locked, received=datetime(2026, 8, 3, 9, 0, tzinfo=UTC))

    collection.run([email])

    history = trail_of(collection, content_hash(locked))
    assert kinds(history) == ["received", "classified", "found", "unopened", "filed", "held"]
    assert entry(history, "unopened").details == {"problem": "the PDF is password-protected"}
    assert entry(history, "filed").details["pending"] is True
    assert history.state == "needs_review"
    # Nothing was read, but its history is whole.
    assert history.recorded_before_trail is False
