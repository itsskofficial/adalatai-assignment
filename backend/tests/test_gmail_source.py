"""The Gmail mail source, checked against recorded responses without calling Google."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

import pytest
from conftest import ReplayServer
from google.auth.exceptions import TransportError
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build  # pyright: ignore[reportUnknownVariableType]
from googleapiclient.errors import HttpError
from googleapiclient.http import HttpMockSequence
from httplib2 import Response

from invoice_collector.domain import Attachment
from invoice_collector.gmail_source import DEFAULT_QUERY, SCOPES, GmailMailSource
from invoice_collector.google_auth import SignInExpired, gmail_service
from invoice_collector.mail_source import MailSource, SourceAccountUnavailable

RECORDED = Path(__file__).parent / "recorded"
ACCOUNT = "finance@acme.test"
AUGUST = (datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 9, 1, tzinfo=UTC))
SLACK, FIGMA, NOTION = "198a7c3e5d2f4b10", "198f01b2c3d4e5f6", "198ef9a8b7c6d5e4"


def recorded(name: str) -> tuple[dict[str, str], str]:
    return {"status": "200"}, (RECORDED / "gmail" / f"{name}.json").read_text("utf-8")


class Replayed:
    """A Gmail mail source answering from recorded responses, remembering what was asked."""

    def __init__(self, *names: str, query: str = DEFAULT_QUERY) -> None:
        self._http = HttpMockSequence([recorded(name) for name in names])
        service = cast(Any, build("gmail", "v1", http=self._http))
        self.source = GmailMailSource(ACCOUNT, service, query=query)

    @property
    def asked(self) -> list[tuple[str, dict[str, list[str]]]]:
        """The path and the parameters of each request, in order."""
        requests = cast(
            "list[tuple[str, ...]]",
            self._http.request_sequence,  # pyright: ignore[reportUnknownMemberType]
        )
        urls = [urlparse(request[0]) for request in requests]
        return [(url.path, parse_qs(url.query)) for url in urls]


def whole_mailbox() -> Replayed:
    return Replayed(
        "messages_page_1",
        "messages_page_2",
        f"message_{SLACK}",
        f"message_{FIGMA}",
        f"message_{NOTION}",
    )


def test_gmail_is_a_mail_source_for_its_source_account() -> None:
    source: MailSource = Replayed().source

    assert source.source_account == ACCOUNT


def test_only_read_only_gmail_access_is_requested() -> None:
    assert SCOPES == ("https://www.googleapis.com/auth/gmail.readonly",)


def test_email_in_the_window_is_returned_with_its_bodies_and_attachments() -> None:
    slack = Replayed("messages_page_2", f"message_{SLACK}").source.emails_between(*AUGUST)[0]

    assert slack.source_account == ACCOUNT
    assert slack.message_id == SLACK
    assert slack.sender == "Slack <feedback@slack.com>"
    assert slack.subject == "Your Slack invoice is available"
    assert slack.received_at == datetime(2026, 8, 3, 3, 46, 12, tzinfo=UTC)
    assert slack.text_body == "Your invoice for August is attached.\n"
    assert slack.html_body == "<p>Your invoice for August is attached.</p>\n"
    assert slack.attachments == (
        Attachment(
            "slack-invoice-2026-08.pdf", "application/pdf", b"%PDF-1.7 recorded Slack invoice"
        ),
    )


def test_email_whose_billing_document_is_the_body_is_returned_with_that_body() -> None:
    [notion] = Replayed("messages_page_2", f"message_{NOTION}").source.emails_between(*AUGUST)

    assert notion.html_body is not None and "Total: $96.00" in notion.html_body
    assert (notion.text_body, notion.attachments) == (None, ())


def test_emails_on_every_page_of_results_are_returned() -> None:
    mailbox = whole_mailbox()

    emails = mailbox.source.emails_between(*AUGUST)

    assert {e.message_id for e in emails} >= {SLACK, NOTION}
    assert "pageToken" not in mailbox.asked[0][1]
    assert mailbox.asked[1][1]["pageToken"] == ["09812345678901234567"]


def test_email_received_outside_the_window_is_excluded() -> None:
    emails = whole_mailbox().source.emails_between(*AUGUST)

    assert [e.message_id for e in emails] == [SLACK, NOTION]


def test_email_received_at_the_end_of_the_window_is_excluded() -> None:
    end = datetime(2026, 8, 31, 23, 59, 30, tzinfo=UTC)

    emails = whole_mailbox().source.emails_between(AUGUST[0], end)

    assert [e.message_id for e in emails] == [SLACK]


def test_search_is_limited_to_the_window_and_to_likely_billing_mail() -> None:
    mailbox = Replayed("messages_none")

    mailbox.source.emails_between(*AUGUST)

    [(path, asked)] = mailbox.asked
    assert path.endswith("/users/me/messages")
    assert asked["q"] == [f"after:1785542400 before:1788220800 {DEFAULT_QUERY}"]
    assert "invoice" in DEFAULT_QUERY and '"credit note"' in DEFAULT_QUERY


def test_search_for_billing_mail_can_be_replaced() -> None:
    mailbox = Replayed("messages_none", query="from:billing@vendor.test")

    mailbox.source.emails_between(*AUGUST)

    assert mailbox.asked[0][1]["q"] == [
        "after:1785542400 before:1788220800 from:billing@vendor.test"
    ]


def test_window_without_a_time_zone_is_rejected() -> None:
    with pytest.raises(ValueError, match="time zone"):
        Replayed().source.emails_between(datetime(2026, 8, 1), datetime(2026, 9, 1))


def test_mailbox_with_no_matching_mail_gives_no_emails() -> None:
    assert Replayed("messages_none").source.emails_between(*AUGUST) == []


def test_each_email_is_fetched_whole() -> None:
    mailbox = Replayed("messages_page_2", f"message_{NOTION}")

    mailbox.source.emails_between(*AUGUST)

    path, asked = mailbox.asked[1]
    assert path.endswith(f"/users/me/messages/{NOTION}")
    assert asked["format"] == ["raw"]


def test_expired_sign_in_is_reported_naming_the_source_account(
    replay_server: ReplayServer,
) -> None:
    revoked = json.loads((RECORDED / "google_sign_in_revoked.json").read_text("utf-8"))
    expired = Credentials(  # pyright: ignore[reportUnknownVariableType]
        token=None,
        refresh_token="stored-refresh-value",
        token_uri=replay_server(400, revoked),
        client_id="made-up-client.apps.googleusercontent.com",
        client_secret="made-up",
        scopes=list(SCOPES),
    )
    source = GmailMailSource(ACCOUNT, gmail_service(expired))

    with pytest.raises(SignInExpired, match=ACCOUNT) as raised:
        source.emails_between(*AUGUST)

    assert raised.value.source_account == ACCOUNT


def test_source_account_that_never_signed_in_is_reported(tmp_path: Path) -> None:
    with pytest.raises(SignInExpired, match=ACCOUNT):
        GmailMailSource.signed_in(ACCOUNT, token_dir=tmp_path)


class Unreachable:
    """A Gmail client whose every call fails in the way given."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def users(self) -> "Unreachable":
        return self

    def messages(self) -> "Unreachable":
        return self

    def list(self, **asked: object) -> "Unreachable":
        return self

    def execute(self, num_retries: int = 0) -> dict[str, Any]:
        raise self._error


OPS = "ops@acme.test"
WINDOW = (datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 9, 1, tzinfo=UTC))


def test_mailbox_that_cannot_be_reached_is_reported_as_unavailable() -> None:
    source = GmailMailSource(OPS, Unreachable(TransportError("connection reset")))

    with pytest.raises(SourceAccountUnavailable, match="ops@acme.test could not be read"):
        source.emails_between(*WINDOW)


def test_error_from_gmail_is_reported_as_unavailable() -> None:
    refused = HttpError(Response({"status": "503"}), b"backend error")
    source = GmailMailSource(OPS, Unreachable(refused))

    with pytest.raises(SourceAccountUnavailable, match="ops@acme.test could not be read"):
        source.emails_between(*WINDOW)


def test_expired_sign_in_is_a_source_account_that_is_unavailable() -> None:
    assert issubclass(SignInExpired, SourceAccountUnavailable)
