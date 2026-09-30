"""Compares what was collected with what was expected.

A gap is an expected vendor with no billing document in the collection month. Whether it
is reported as missing or unknown depends on whether the mail could be read: a vendor
whose source account failed to sync may have sent its invoice to mail nobody has read.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from statistics import median

from invoice_collector.checks import summary_of
from invoice_collector.domain import (
    BillingSignal,
    CollectionMonth,
    EmailState,
    ExpectedVendor,
    Gap,
    SummaryRow,
    UpcomingCharge,
)
from invoice_collector.ledger import ExaminedEmail, Ledger, PendingDocument

# The reason a run gives an email whose billing document is behind a portal link that
# needs a sign-in. A person downloads it and uploads it on the Review screen.
MANUAL_DOWNLOAD_NEEDED = "manual download needed"
BEHIND_A_SIGN_IN = (
    "its invoice is behind a portal that needs a sign-in: "
    "download it and upload it on the Review screen"
)

# Legal forms only. A word such as Labs or Systems is part of the name: Acme Labs and
# Acme Systems are two vendors.
_LEGAL_SUFFIXES = re.compile(
    r"\b(inc|incorporated|ltd|limited|llc|llp|gmbh|corp|corporation|co|pvt|plc|pty|bv|sa)\b"
)


def vendor_key(vendor: str) -> str:
    """The form in which two spellings of one vendor are equal."""
    words = re.sub(r"[^a-z0-9 ]+", " ", vendor.casefold())
    trimmed = " ".join(_LEGAL_SUFFIXES.sub(" ", words).split())
    return (trimmed or " ".join(words.split())).replace(" ", "")


@dataclass(frozen=True)
class Reconciliation:
    gaps: list[Gap] = field(default_factory=list[Gap])
    upcoming: list[UpcomingCharge] = field(default_factory=list[UpcomingCharge])


def _is_due(vendor: ExpectedVendor, month: CollectionMonth) -> bool:
    if vendor.billing_cycle == "annual":
        return vendor.renewal_month == month.month
    return True


def unsettled(emails: Sequence[ExaminedEmail]) -> list[ExaminedEmail]:
    """Emails nothing was collected from that were held or failed: those waiting for a
    manual download, and those that failed with a reason."""
    return [
        e
        for e in emails
        if e.state is EmailState.FAILED
        or (e.state is EmailState.NEEDS_REVIEW and e.reason == MANUAL_DOWNLOAD_NEEDED)
    ]


def _elsewhere(vendor: ExpectedVendor, email: ExaminedEmail) -> str:
    """Names the mailbox an email came to when it is not the one the vendor is expected in."""
    if vendor.source_account is None or email.source_account == vendor.source_account:
        return ""
    return f" in {email.source_account}"


def _explanations(
    vendor: ExpectedVendor,
    signals: Sequence[BillingSignal],
    held: Sequence[PendingDocument],
    emails: Sequence[ExaminedEmail],
) -> list[str]:
    """What explains a gap: a billing document of the vendor waiting for a person, an
    email of the vendor waiting for a manual download or that failed, and a payment that
    failed."""
    found: list[str] = []
    key = vendor_key(vendor.vendor)
    # A credit note does not stand in for the invoice, so it does not explain its absence.
    waiting = [
        d
        for d in held
        if d.extraction.document_type != "credit_note" and vendor_key(d.extraction.vendor) == key
    ]
    if waiting:
        # A document held only because another in its email was doubted has no doubts.
        doubts = next((d.doubts for d in waiting if d.doubts), ())
        found.append(f"held for review: {summary_of(doubts)}" if doubts else "held for review")
    # A credit note does not explain a missing invoice, whatever became of its email.
    theirs = [
        e
        for e in unsettled(emails)
        if e.vendor and vendor_key(e.vendor) == key and e.kind != "credit_note"
    ]
    # An email of the vendor in another mailbox than the one it is expected to bill still
    # explains the gap, since vendors send to whichever address they hold. It says where.
    waiting_for_a_person = [e for e in theirs if e.state is EmailState.NEEDS_REVIEW]
    if waiting_for_a_person:
        found.append(BEHIND_A_SIGN_IN + _elsewhere(vendor, waiting_for_a_person[-1]))
    failures = [e for e in theirs if e.state is EmailState.FAILED]
    if failures:
        # The latest, as for a payment that failed. Emails are in the order they arrived.
        latest = failures[-1]
        reason = latest.reason or "no reason was given"
        found.append(f"an email from it{_elsewhere(vendor, latest)} failed: {reason}")
    failed = [
        s
        for s in signals
        if s.kind == "payment_failed" and s.vendor and vendor_key(s.vendor) == key
    ]
    if failed:
        latest = max(failed, key=lambda s: s.received_at)
        found.append(f"payment failed on {latest.received_at.day} {latest.received_at:%B}")
    return found


def reconcile(
    month: CollectionMonth,
    expected: Sequence[ExpectedVendor],
    charges: Sequence[SummaryRow],
    signals: Sequence[BillingSignal],
    failed_source_accounts: Mapping[str, str],
    held: Sequence[PendingDocument] = (),
    emails: Sequence[ExaminedEmail] = (),
) -> Reconciliation:
    """Gaps against the expected vendors, and upcoming charges.

    held are the month's billing documents waiting for a person to confirm. They are not
    collected, so their vendor is still a gap, and they explain it. emails are the emails
    the month examined; one of the vendor that waits for a manual download, or that
    failed, explains its gap too.
    """
    # Money returned is not the invoice that was expected.
    collected = {vendor_key(row.vendor) for row in charges if row.document_type != "credit_note"}
    gaps: list[Gap] = []
    for vendor in expected:
        if vendor.status != "expected" or not _is_due(vendor, month):
            continue
        if vendor_key(vendor.vendor) in collected:
            continue

        # With no source account named, the invoice could have gone to any of them.
        if vendor.source_account is None:
            unread = sorted(failed_source_accounts)
        elif vendor.source_account in failed_source_accounts:
            unread = [vendor.source_account]
        else:
            unread = []
        explanations = _explanations(vendor, signals, held, emails)
        if unread:
            explanations.insert(0, f"{', '.join(unread)} could not be read")
        gaps.append(
            Gap(
                vendor=vendor.vendor,
                kind="unknown" if unread else "missing",
                source_account=vendor.source_account,
                explanation="; ".join(explanations) or None,
            )
        )

    upcoming = [
        UpcomingCharge(
            vendor=s.vendor or "an unnamed vendor", source_account=s.source_account, note=s.subject
        )
        for s in signals
        if s.kind == "renewal_reminder"
    ]
    return Reconciliation(sorted(gaps, key=lambda g: g.vendor.casefold()), upcoming)


def suggested_vendors(
    known: Sequence[ExpectedVendor], charges: Sequence[SummaryRow]
) -> list[ExpectedVendor]:
    """Vendors that have billed the company and are on no list, as suggestions."""
    listed = {vendor_key(v.vendor) for v in known}
    seen: dict[str, list[SummaryRow]] = {}
    for row in charges:
        if row.document_type != "credit_note" and vendor_key(row.vendor) not in listed:
            seen.setdefault(vendor_key(row.vendor), []).append(row)

    suggestions: list[ExpectedVendor] = []
    for rows in seen.values():
        latest = max(rows, key=lambda r: r.invoice_date)
        same_currency = [r.total for r in rows if r.currency == latest.currency]
        suggestions.append(
            ExpectedVendor(
                vendor=latest.vendor,
                source_account=latest.source_accounts[0] if latest.source_accounts else None,
                billing_cycle="monthly",
                renewal_month=None,
                usual_amount=Decimal(median(same_currency)).quantize(Decimal("0.01")),
                currency=latest.currency,
                status="suggested",
            )
        )
    return sorted(suggestions, key=lambda v: v.vendor.casefold())


def reconcile_month(
    ledger: Ledger, month: CollectionMonth, charges: Sequence[SummaryRow]
) -> Reconciliation:
    """Reads what is needed from the ledger. Used by the run and by the dashboard."""
    failed = {s.source_account: s.reason or "" for s in ledger.syncs(month) if not s.succeeded}
    return reconcile(
        month,
        ledger.expected_vendors(),
        charges,
        ledger.billing_signals(month),
        failed,
        ledger.pending(month),
        ledger.examined_emails(month),
    )
