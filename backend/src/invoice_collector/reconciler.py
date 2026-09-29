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
    ExpectedVendor,
    Gap,
    SummaryRow,
    UpcomingCharge,
)
from invoice_collector.ledger import Ledger, PendingDocument

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


def _explanations(
    vendor: ExpectedVendor, signals: Sequence[BillingSignal], held: Sequence[PendingDocument]
) -> list[str]:
    """What explains a gap: a billing document of the vendor waiting for a person, and a
    payment that failed."""
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
) -> Reconciliation:
    """Gaps against the expected vendors, and upcoming charges.

    held are the month's billing documents waiting for a person to confirm. They are not
    collected, so their vendor is still a gap, and they explain it.
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
        explanations = _explanations(vendor, signals, held)
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
    )
