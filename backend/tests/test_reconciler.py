"""The status of a gap: which of its causes names it, and in what order they rank."""

from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from invoice_collector.domain import (
    BillingSignal,
    CollectionMonth,
    EmailState,
    ExpectedVendor,
    Extraction,
    Gap,
    GapStatus,
    InvoiceFormat,
)
from invoice_collector.ledger import ExaminedEmail, PendingDocument
from invoice_collector.reconciler import MANUAL_DOWNLOAD_NEEDED, reconcile

AUGUST = CollectionMonth(2026, 8)
MAILBOX = "ops@nyayalabs.example"
VENDOR = ExpectedVendor("Zoom", MAILBOX, "monthly", None, None, "USD")


def held() -> PendingDocument:
    extraction = Extraction("invoice", "Zoom", date(2026, 8, 3), Decimal("15.00"), "USD")
    return PendingDocument("hash-zoom", extraction, "pending/zoom.pdf", ())


def manual_download() -> ExaminedEmail:
    return ExaminedEmail(
        MAILBOX,
        "m-portal",
        "Your Zoom invoice is available",
        EmailState.NEEDS_REVIEW,
        MANUAL_DOWNLOAD_NEEDED,
        InvoiceFormat.PORTAL_LINK,
        "https://zoom.example/billing",
        vendor="Zoom",
        kind="invoice",
    )


def failed_email() -> ExaminedEmail:
    return ExaminedEmail(
        MAILBOX,
        "m-failed",
        "Your Zoom invoice",
        EmailState.FAILED,
        "the PDF could not be opened",
        InvoiceFormat.ATTACHMENT,
        None,
        vendor="Zoom",
        kind="invoice",
    )


def payment_failed() -> BillingSignal:
    return BillingSignal(
        "payment_failed",
        "Zoom",
        MAILBOX,
        "m-payment",
        "Your payment to Zoom failed",
        datetime(2026, 8, 12, 9, 0, tzinfo=UTC),
    )


def gap(
    *,
    signals: Sequence[BillingSignal] = (),
    pending: Sequence[PendingDocument] = (),
    emails: Sequence[ExaminedEmail] = (),
    unread: bool = False,
) -> Gap:
    failed = {MAILBOX: "sign-in expired"} if unread else {}
    [found] = reconcile(AUGUST, [VENDOR], [], signals, failed, pending, emails).gaps
    return found


def test_gap_nothing_explains_was_not_received() -> None:
    found = gap()

    assert (found.kind, found.status, found.explanation) == ("missing", "not_received", None)


@pytest.mark.parametrize(
    ("found", "status"),
    [
        (lambda: gap(pending=[held()]), "held_for_review"),
        (lambda: gap(emails=[manual_download()]), "manual_download"),
        (lambda: gap(emails=[failed_email()]), "email_failed"),
        (lambda: gap(signals=[payment_failed()]), "payment_failed"),
        (lambda: gap(unread=True), "mailbox_unread"),
    ],
    ids=["held", "manual download", "email failed", "payment failed", "mailbox unread"],
)
def test_status_of_a_gap_names_its_cause(found: Callable[[], Gap], status: GapStatus) -> None:
    assert found().status == status


def test_mailbox_that_could_not_be_read_is_exactly_the_unknown_kind() -> None:
    assert gap(unread=True).kind == "unknown"
    assert gap(signals=[payment_failed()]).kind == "missing"


def test_held_document_outranks_every_other_cause() -> None:
    found = gap(
        pending=[held()],
        emails=[manual_download(), failed_email()],
        signals=[payment_failed()],
        unread=True,
    )

    assert (found.kind, found.status) == ("unknown", "held_for_review")
    # The explanation still says every cause, in the order it always has.
    assert found.explanation == (
        f"{MAILBOX} could not be read; held for review; "
        "its invoice is behind a portal that needs a sign-in: "
        "download it and upload it on the Review screen; "
        "an email from it failed: the PDF could not be opened; "
        "payment failed on 12 August"
    )


def test_email_waiting_for_a_manual_download_outranks_a_failed_one() -> None:
    found = gap(emails=[failed_email(), manual_download()], signals=[payment_failed()])

    assert found.status == "manual_download"


def test_failed_email_outranks_a_failed_payment() -> None:
    found = gap(emails=[failed_email()], signals=[payment_failed()], unread=True)

    assert found.status == "email_failed"


def test_failed_payment_outranks_a_mailbox_that_could_not_be_read() -> None:
    found = gap(signals=[payment_failed()], unread=True)

    assert (found.kind, found.status) == ("unknown", "payment_failed")
    assert found.explanation == f"{MAILBOX} could not be read; payment failed on 12 August"
