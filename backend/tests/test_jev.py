"""The Jev classifier and the vendor matchers, checked against recorded responses.

The recorded responses are written by hand to the documented response shape. Jev is never called.
"""

import json
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from invoice_collector.classifier import ClassificationFailed
from invoice_collector.domain import Classification, Email
from invoice_collector.jev_classifier import JevClassifier
from invoice_collector.vendor_matcher import (
    NONE_OF_THESE,
    FakeVendorMatcher,
    JevVendorMatcher,
    RuleVendorMatcher,
    VendorMatch,
    VendorMatchFailed,
)

RECORDED_DIR = Path(__file__).parent / "recorded"
KIND: dict[str, Any] = json.loads((RECORDED_DIR / "jev_payment_failed.json").read_text("utf-8"))
VENDOR: dict[str, Any] = json.loads((RECORDED_DIR / "jev_vendor_match.json").read_text("utf-8"))

NOTICE = Email(
    source_account="ops@nyayalabs.example",
    message_id="m-failed-1",
    sender="Notion <team@mail.notion.so>",
    subject="Your payment failed",
    received_at=datetime(2026, 8, 12, 4, 0, tzinfo=UTC),
    text_body="We could not process your payment of $190.00 for Notion Plus.",
)
EXPECTED_VENDORS = ("Slack", "Notion", "Figma")
INVOICE_TEXT = "Invoice INV-2041 from Slack Technologies for the Pro plan."


@dataclass
class Seen:
    """What the local server received."""

    path: str = ""
    authorization: str | None = None
    bodies: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])


@dataclass(frozen=True)
class JevServer:
    base_url: str
    seen: Seen


StartJev = Callable[[int, object], JevServer]


@pytest.fixture
def jev_server() -> Iterator[StartJev]:
    """A local server standing in for Jev that replays one recorded response."""
    servers: list[ThreadingHTTPServer] = []

    def start(status: int, body: object) -> JevServer:
        seen = Seen()
        payload = body if isinstance(body, bytes) else json.dumps(body).encode()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                pass

            def do_POST(self) -> None:
                request = self.rfile.read(int(self.headers["Content-Length"]))
                seen.path = self.path
                seen.authorization = self.headers["Authorization"]
                seen.bodies.append(json.loads(request))
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return JevServer(f"http://127.0.0.1:{server.server_port}", seen)

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def classifier_at(server: JevServer) -> JevClassifier:
    return JevClassifier("not-a-real-key", base_url=server.base_url, max_retries=0)


def matcher_at(server: JevServer) -> JevVendorMatcher:
    return JevVendorMatcher("not-a-real-key", base_url=server.base_url, max_retries=0)


def choosing(recorded: dict[str, Any], choice: str, probability: float) -> dict[str, Any]:
    """The recorded response, with the chosen option and its probability replaced."""
    (name, answer), *_ = recorded["answers"].items()
    probabilities = {**answer["probabilities"], choice: probability}
    changed = {**answer, "choice": choice, "probabilities": probabilities}
    return {**recorded, "answers": {name: changed}}


def test_jev_classifies_a_payment_failed_notice(jev_server: StartJev) -> None:
    server = jev_server(200, KIND)

    classification = classifier_at(server).classify(NOTICE)

    assert classification == Classification("payment_failed", "Notion", "high", 0.96, "jev-latest")


def test_jev_is_asked_one_choice_over_the_six_email_kinds(jev_server: StartJev) -> None:
    server = jev_server(200, KIND)

    classifier_at(server).classify(NOTICE)

    assert server.seen.path == "/v1/systemone"
    assert server.seen.authorization == "Bearer not-a-real-key"
    (body,) = server.seen.bodies
    assert body["model"] == "jev-latest"
    assert "We could not process your payment" in json.dumps(body["state"])
    question = body["questions"]["kind"]
    assert question["type"] == "choice"
    assert list(question["criteria"]) == [
        "invoice",
        "receipt",
        "credit_note",
        "payment_failed",
        "renewal_reminder",
        "not_billing",
    ]
    assert question["criteria"]["credit_note"] == "A vendor records money returned."


def test_email_that_is_not_about_billing_has_no_vendor(jev_server: StartJev) -> None:
    server = jev_server(200, choosing(KIND, "not_billing", 0.97))

    assert classifier_at(server).classify(NOTICE) == Classification(
        "not_billing", None, "high", 0.97, "jev-latest"
    )


@pytest.mark.parametrize(
    ("probability", "confidence"),
    [
        (0.90, "high"),
        (0.8999, "medium"),
        (0.70, "medium"),
        (0.6999, "low"),
        (0.0, "low"),
    ],
)
def test_probability_decides_confidence(
    jev_server: StartJev, probability: float, confidence: str
) -> None:
    server = jev_server(200, choosing(KIND, "payment_failed", probability))

    classification = classifier_at(server).classify(NOTICE)

    assert classification.confidence == confidence
    assert classification.probability == probability


def test_error_from_jev_fails(jev_server: StartJev) -> None:
    server = jev_server(529, {"error": "Overloaded"})

    with pytest.raises(ClassificationFailed, match="HTTP 529"):
        classifier_at(server).classify(NOTICE)


def test_jev_that_cannot_be_reached_fails(jev_server: StartJev) -> None:
    classifier = JevClassifier("not-a-real-key", base_url="http://127.0.0.1:9", max_retries=0)

    with pytest.raises(ClassificationFailed, match="could not reach"):
        classifier.classify(NOTICE)


@pytest.mark.parametrize(
    "body",
    [
        b"<html>not json</html>",
        ["not", "an", "object"],
        {"model": "jev-1.13.0", "usage": {"input_tokens": 1, "output_tokens": 1}},
        {**KIND, "answers": {"kind": {"type": "choice", "choice": "invoice"}}},
        {**KIND, "answers": {"kind": {"type": "noul", "noul": 0.9}}},
        {
            **KIND,
            "answers": {
                "kind": {
                    "type": "choice",
                    "choice": "invoice",
                    "confidence": 0.9,
                    "probabilities": {"receipt": 1.0},
                }
            },
        },
    ],
    ids=[
        "not json",
        "not an object",
        "no answers",
        "no probabilities",
        "wrong answer type",
        "no probability for the choice",
    ],
)
def test_malformed_response_fails(jev_server: StartJev, body: object) -> None:
    server = jev_server(200, body)

    with pytest.raises(ClassificationFailed, match="malformed"):
        classifier_at(server).classify(NOTICE)


def test_kind_that_was_not_offered_fails(jev_server: StartJev) -> None:
    server = jev_server(200, choosing(KIND, "statement", 0.95))

    with pytest.raises(ClassificationFailed, match="not one of the options offered: statement"):
        classifier_at(server).classify(NOTICE)


def test_jev_matches_an_expected_vendor(jev_server: StartJev) -> None:
    server = jev_server(200, VENDOR)

    match = matcher_at(server).match(INVOICE_TEXT, EXPECTED_VENDORS)

    assert match == VendorMatch("Slack", 0.93)
    assert match.by == "jev-latest"
    (body,) = server.seen.bodies
    question = body["questions"]["vendor"]
    assert question["type"] == "choice"
    assert list(question["criteria"]) == [*EXPECTED_VENDORS, NONE_OF_THESE]
    assert body["state"] == INVOICE_TEXT


def test_vendor_not_on_the_list_matches_none(jev_server: StartJev) -> None:
    server = jev_server(200, choosing(VENDOR, NONE_OF_THESE, 0.88))

    match = matcher_at(server).match("Invoice from Linear Orbit, Inc.", EXPECTED_VENDORS)

    assert match == VendorMatch(None, 0.88)


def test_more_than_254_expected_vendors_are_refused(jev_server: StartJev) -> None:
    server = jev_server(200, VENDOR)
    vendors = [f"Vendor {number}" for number in range(255)]

    with pytest.raises(VendorMatchFailed, match="at most 254 expected vendors, got 255"):
        matcher_at(server).match(INVOICE_TEXT, vendors)

    assert server.seen.bodies == []


def test_254_expected_vendors_are_accepted(jev_server: StartJev) -> None:
    server = jev_server(200, VENDOR)
    vendors = ["Slack", *(f"Vendor {number}" for number in range(253))]

    assert matcher_at(server).match(INVOICE_TEXT, vendors).vendor == "Slack"


def test_no_expected_vendors_matches_none_without_asking(jev_server: StartJev) -> None:
    server = jev_server(200, VENDOR)

    assert matcher_at(server).match(INVOICE_TEXT, []) == VendorMatch(None)
    assert server.seen.bodies == []


def test_vendor_that_was_not_offered_fails(jev_server: StartJev) -> None:
    server = jev_server(200, choosing(VENDOR, "Linear", 0.95))

    with pytest.raises(VendorMatchFailed, match="not one of the options offered: Linear"):
        matcher_at(server).match(INVOICE_TEXT, EXPECTED_VENDORS)


def test_error_from_jev_fails_the_vendor_match(jev_server: StartJev) -> None:
    server = jev_server(401, {"error": "Invalid API key"})

    with pytest.raises(VendorMatchFailed, match="HTTP 401"):
        matcher_at(server).match(INVOICE_TEXT, EXPECTED_VENDORS)


def test_malformed_response_fails_the_vendor_match(jev_server: StartJev) -> None:
    server = jev_server(200, {"answers": "none"})

    with pytest.raises(VendorMatchFailed, match="malformed"):
        matcher_at(server).match(INVOICE_TEXT, EXPECTED_VENDORS)


def test_rules_match_a_vendor_named_in_the_text() -> None:
    match = RuleVendorMatcher().match("INVOICE from SLACK Technologies", EXPECTED_VENDORS)

    assert match == VendorMatch("Slack")
    assert match.by == "rules"


def test_rules_match_whole_words_only() -> None:
    match = RuleVendorMatcher().match("We are slacking on the notional plan", EXPECTED_VENDORS)

    assert match == VendorMatch(None)


def test_rules_match_none_when_several_vendors_are_named() -> None:
    match = RuleVendorMatcher().match("Slack invoice, paid via Notion", EXPECTED_VENDORS)

    assert match == VendorMatch(None)


def test_fake_matcher_returns_prepared_answers_and_fails_on_request() -> None:
    fake = FakeVendorMatcher({"text a": VendorMatch("Figma", 0.8)}, failing=frozenset({"text b"}))

    assert fake.match("text a", EXPECTED_VENDORS) == VendorMatch("Figma", 0.8)
    assert fake.match("A Notion receipt", EXPECTED_VENDORS) == VendorMatch("Notion")
    with pytest.raises(VendorMatchFailed):
        fake.match("text b", EXPECTED_VENDORS)


def characters_sent(body: dict[str, Any]) -> int:
    return len(json.dumps([body["state"], body["questions"]], ensure_ascii=False))


def test_long_email_is_cut_so_the_whole_request_stays_within_the_limit(
    jev_server: StartJev,
) -> None:
    server = jev_server(200, KIND)
    long_email = replace(NOTICE, subject="Payment failed " * 500, text_body="बिल " * 40_000)

    classifier_at(server).classify(long_email)

    (body,) = server.seen.bodies
    assert body["state"]["subject"] == long_email.subject
    assert "बिल बिल" in body["state"]["body"]
    assert characters_sent(body) <= 24_500


def test_long_document_is_cut_to_leave_room_for_the_expected_vendors(
    jev_server: StartJev,
) -> None:
    server = jev_server(200, VENDOR)
    vendors = ("Slack", *(f"A vendor with a long name, number {n}" for n in range(200)))

    matcher_at(server).match("Invoice from Slack. " * 5_000, vendors)

    (body,) = server.seen.bodies
    assert list(body["questions"]["vendor"]["criteria"])[:1] == ["Slack"]
    assert len(body["questions"]["vendor"]["criteria"]) == 202
    assert body["state"].startswith("Invoice from Slack.")
    # The measure also counts the punctuation of JSON around each of the 202 options.
    assert characters_sent(body) <= 28_000


@pytest.mark.parametrize("without_a_name", ["", "   "])
def test_rules_pass_over_an_expected_vendor_without_a_name(without_a_name: str) -> None:
    match = RuleVendorMatcher().match("Invoice: Slack", ("Slack", without_a_name))

    assert match == VendorMatch("Slack")


def test_jev_is_not_offered_an_expected_vendor_without_a_name(jev_server: StartJev) -> None:
    server = jev_server(200, VENDOR)

    matcher_at(server).match(INVOICE_TEXT, ("Slack", "", "Notion", " "))

    (body,) = server.seen.bodies
    assert list(body["questions"]["vendor"]["criteria"]) == ["Slack", "Notion", NONE_OF_THESE]


def test_email_too_large_without_its_body_is_not_sent(jev_server: StartJev) -> None:
    server = jev_server(200, KIND)
    too_large = replace(NOTICE, subject="Payment failed " * 2_000)

    with pytest.raises(ClassificationFailed, match="too large for Jev"):
        classifier_at(server).classify(too_large)

    assert server.seen.bodies == []


def test_expected_vendors_too_large_together_are_not_sent(jev_server: StartJev) -> None:
    server = jev_server(200, VENDOR)
    vendors = [f"A vendor {n} " + "with a long name " * 10 for n in range(200)]

    with pytest.raises(VendorMatchFailed, match="too large for Jev"):
        matcher_at(server).match(INVOICE_TEXT, vendors)

    assert server.seen.bodies == []
