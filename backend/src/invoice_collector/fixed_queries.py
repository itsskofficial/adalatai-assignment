"""The fixed set of queries "Ask your invoices" chooses from (ADR 0007).

Each query is a plain function over charges. The model picks one and fills in its
parameters; it never writes a query of its own and never sees an amount.

Rupee totals leave out charges with no rupee amount; each result keeps every charge it
matched so those can be reported. A charge found in several source accounts is attributed
to the first, as on the Spend screen, so totals by source account add up.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal

from invoice_collector.charge_history import Charge, months_from
from invoice_collector.domain import CollectionMonth, DocumentType

ZERO = Decimal("0.00")
DOCUMENT_TYPES: tuple[DocumentType, ...] = ("invoice", "receipt", "credit_note")


@dataclass(frozen=True)
class Period:
    """Collection months from the first to the last, both included."""

    first: CollectionMonth
    last: CollectionMonth

    def contains(self, month: CollectionMonth) -> bool:
        return str(self.first) <= str(month) <= str(self.last)

    def months(self) -> list[CollectionMonth]:
        return months_from(self.first, self.last)


@dataclass(frozen=True)
class RupeeTotal:
    inr_total: Decimal
    charges: tuple[Charge, ...]

    @property
    def without_rupees(self) -> tuple[Charge, ...]:
        return tuple(charge for charge in self.charges if charge.inr_total is None)


@dataclass(frozen=True)
class GroupTotal:
    """Rupee spend of one vendor or one collection month."""

    key: str
    inr_total: Decimal
    charges: tuple[Charge, ...]


@dataclass(frozen=True)
class NewVendor:
    vendor: str
    first_month: CollectionMonth
    charges: tuple[Charge, ...]


@dataclass(frozen=True)
class DocumentTypeCount:
    document_type: DocumentType
    count: int
    charges: tuple[Charge, ...]


def _same(a: str, b: str) -> bool:
    return a.casefold() == b.casefold()


def _in(period: Period | None) -> Callable[[Charge], bool]:
    return lambda charge: period is None or period.contains(charge.collection_month)


def _rupees(charges: Sequence[Charge]) -> Decimal:
    return sum((charge.inr_total or ZERO for charge in charges), ZERO)


def _by_date(charges: Sequence[Charge]) -> tuple[Charge, ...]:
    return tuple(sorted(charges, key=lambda c: (c.invoice_date, c.vendor.casefold())))


def total_spend(
    charges: Sequence[Charge],
    *,
    vendor: str | None = None,
    source_account: str | None = None,
    period: Period | None = None,
) -> RupeeTotal:
    """Total rupee spend, optionally on one vendor, from one source account, in a period."""
    matched = [
        charge
        for charge in charges
        if _in(period)(charge)
        and (vendor is None or _same(charge.vendor, vendor))
        and (source_account is None or _same(charge.first_source_account, source_account))
    ]
    return RupeeTotal(inr_total=_rupees(matched), charges=_by_date(matched))


def spend_by_vendor(charges: Sequence[Charge], period: Period) -> tuple[GroupTotal, ...]:
    """Rupee spend per vendor in a period, largest first."""
    groups: dict[str, list[Charge]] = {}
    for charge in filter(_in(period), charges):
        if charge.inr_total is not None:
            groups.setdefault(charge.vendor, []).append(charge)
    totals = [GroupTotal(v, _rupees(group), _by_date(group)) for v, group in groups.items()]
    return tuple(sorted(totals, key=lambda each: (-each.inr_total, each.key.casefold())))


def spend_by_month(
    charges: Sequence[Charge], vendor: str, period: Period
) -> tuple[GroupTotal, ...]:
    """Rupee spend on one vendor in each collection month of a period, oldest first."""
    of_vendor = [c for c in charges if _same(c.vendor, vendor) and c.inr_total is not None]
    totals: list[GroupTotal] = []
    for month in period.months():
        group = [charge for charge in of_vendor if charge.collection_month == month]
        totals.append(GroupTotal(str(month), _rupees(group), _by_date(group)))
    return tuple(totals)


def largest_charges(
    charges: Sequence[Charge], period: Period, limit: int = 5
) -> tuple[Charge, ...]:
    """The charges with the largest rupee amounts in a period."""
    ranked = sorted(
        (c for c in charges if _in(period)(c) and c.inr_total is not None),
        key=lambda c: (-(c.inr_total or ZERO), c.invoice_date, c.vendor.casefold()),
    )
    return tuple(ranked[:limit])


def vendor_charges(charges: Sequence[Charge], vendor: str, period: Period) -> tuple[Charge, ...]:
    """Every charge from one vendor in a period, by invoice date."""
    return _by_date([c for c in charges if _in(period)(c) and _same(c.vendor, vendor)])


def new_vendors(charges: Sequence[Charge], period: Period) -> tuple[NewVendor, ...]:
    """Vendors whose first charge in the given charges falls in the period, by name."""
    first_seen: dict[str, CollectionMonth] = {}
    names: dict[str, str] = {}
    for charge in sorted(charges, key=lambda c: c.month):
        key = charge.vendor.casefold()
        first_seen.setdefault(key, charge.collection_month)
        names.setdefault(key, charge.vendor)
    return tuple(
        NewVendor(
            vendor=names[key],
            first_month=month,
            charges=_by_date([c for c in charges if _in(period)(c) and c.vendor.casefold() == key]),
        )
        for key, month in sorted(first_seen.items())
        if period.contains(month)
    )


def document_type_counts(
    charges: Sequence[Charge], period: Period
) -> tuple[DocumentTypeCount, ...]:
    """How many billing documents of each document type a period holds.

    An invoice and its receipt are one charge, so they are counted once, as the invoice.
    """
    in_period = [charge for charge in charges if _in(period)(charge)]
    return tuple(
        DocumentTypeCount(
            document_type=kind,
            count=len(group),
            charges=_by_date(group),
        )
        for kind in DOCUMENT_TYPES
        for group in [[c for c in in_period if c.document_type == kind]]
    )
