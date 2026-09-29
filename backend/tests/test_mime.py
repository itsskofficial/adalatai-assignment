"""Raw RFC 822 messages turned into emails."""

from datetime import UTC, datetime, timedelta, timezone
from email.message import EmailMessage

import pytest

from invoice_collector.domain import Attachment
from invoice_collector.mime import email_from_rfc822

PDF = b"%PDF-1.7 any document"


def slack_invoice() -> EmailMessage:
    message = EmailMessage()
    message["From"] = "Slack <feedback@slack.com>"
    message["To"] = "finance@acme.test"
    message["Subject"] = "Your Slack invoice is available"
    message["Date"] = "Mon, 03 Aug 2026 09:15:00 +0530"
    message["Message-ID"] = "<slack-0001@mail.slack.com>"
    message.set_content("Your invoice for August is attached.")
    message.add_alternative("<p>Your invoice for August is attached.</p>", subtype="html")
    message.add_attachment(PDF, maintype="application", subtype="pdf", filename="slack-invoice.pdf")
    return message


def test_email_carries_the_sender_subject_bodies_and_attachments() -> None:
    email = email_from_rfc822("finance@acme.test", slack_invoice().as_bytes())

    assert email.source_account == "finance@acme.test"
    assert email.sender == "Slack <feedback@slack.com>"
    assert email.subject == "Your Slack invoice is available"
    assert email.text_body is not None and "attached" in email.text_body
    assert email.html_body is not None and email.html_body.startswith("<p>")
    assert email.attachments == (Attachment("slack-invoice.pdf", "application/pdf", PDF),)


def test_message_id_and_received_time_default_to_the_headers() -> None:
    email = email_from_rfc822("finance@acme.test", slack_invoice().as_bytes())

    assert email.message_id == "slack-0001@mail.slack.com"
    assert email.received_at == datetime(
        2026, 8, 3, 9, 15, tzinfo=timezone(timedelta(hours=5, minutes=30))
    )


def test_message_id_and_received_time_given_by_the_mailbox_win_over_the_headers() -> None:
    received_at = datetime(2026, 8, 3, 3, 46, tzinfo=UTC)

    email = email_from_rfc822(
        "finance@acme.test",
        slack_invoice().as_bytes(),
        message_id="198a7c3e5d2f4b10",
        received_at=received_at,
    )

    assert (email.message_id, email.received_at) == ("198a7c3e5d2f4b10", received_at)


def test_email_without_a_body_part_has_no_bodies() -> None:
    message = EmailMessage()
    message["From"] = "billing@notion.so"
    message["Subject"] = "Receipt"
    message["Date"] = "Mon, 03 Aug 2026 09:15:00 +0000"
    message.add_attachment(PDF, maintype="application", subtype="pdf", filename="receipt.pdf")

    email = email_from_rfc822("finance@acme.test", message.as_bytes(), message_id="m1")

    assert (email.text_body, email.html_body) == (None, None)
    assert [a.filename for a in email.attachments] == ["receipt.pdf"]


def test_message_with_no_received_time_at_all_is_rejected() -> None:
    message = EmailMessage()
    message["From"] = "billing@notion.so"
    message["Subject"] = "Receipt"
    message.set_content("Thanks for your payment.")

    with pytest.raises(ValueError, match="no Date header"):
        email_from_rfc822("finance@acme.test", message.as_bytes(), message_id="m1")
