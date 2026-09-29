"""What the Spend screen shows: spend in rupees by month, vendor and source account.

Only collected billing documents are charges, so a document awaiting review is not counted.
A credit note's amount is negative, so it reduces every total it falls in. A charge with no
rupee amount is left out of rupee totals and reported with its own currency instead.
"""

from collections.abc import Callable, Sequence
from decimal import ROUND_HALF_UP, Decimal

from pydantic import BaseModel

from invoice_collector.charge_history import Charge, charges_in, month_before
from invoice_collector.domain import CollectionMonth
from invoice_collector.ledger import Ledger

DEFAULT_MONTHS = 6
ZERO = Decimal(0)


class MonthSpend(BaseModel):
    month: str
    inr_total: str


class VendorSpend(BaseModel):
    vendor: str
    inr_total: str


class SourceAccountSpend(BaseModel):
    source_account: str
    inr_total: str


class CurrencyTotal(BaseModel):
    currency: str
    amount: str


class WithoutRupees(BaseModel):
    charges: int
    totals: list[CurrencyTotal]


class VendorChange(BaseModel):
    vendor: str
    previous_inr: str
    current_inr: str
    change_inr: str
    change_percent: str | None


class Changes(BaseModel):
    month: str
    previous_month: str
    vendors: list[VendorChange]


class Spend(BaseModel):
    from_month: str | None
    to_month: str | None
    months: list[MonthSpend]
    vendors: list[VendorSpend]
    source_accounts: list[SourceAccountSpend]
    shared_charges: int
    inr_total: str
    without_rupees: WithoutRupees
    changes: Changes | None


def amount(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}"


def with_rupees(charges: Sequence[Charge]) -> list[Charge]:
    return [charge for charge in charges if charge.inr_total is not None]


def rupee_total(charges: Sequence[Charge]) -> Decimal:
    return sum((charge.inr_total or ZERO for charge in charges), ZERO)


def rupees_by(charges: Sequence[Charge], key: Callable[[Charge], str]) -> dict[str, Decimal]:
    """Rupee totals grouped by a key, largest first, leaving out charges with no rupee amount."""
    totals: dict[str, Decimal] = {}
    for charge in with_rupees(charges):
        totals[key(charge)] = totals.get(key(charge), ZERO) + (charge.inr_total or ZERO)
    return dict(sorted(totals.items(), key=lambda item: (-item[1], item[0].casefold())))


def totals_without_rupees(charges: Sequence[Charge]) -> WithoutRupees:
    missing = [charge for charge in charges if charge.inr_total is None]
    totals: dict[str, Decimal] = {}
    for charge in missing:
        totals[charge.currency] = totals.get(charge.currency, ZERO) + charge.total
    return WithoutRupees(
        charges=len(missing),
        totals=[CurrencyTotal(currency=c, amount=amount(totals[c])) for c in sorted(totals)],
    )


def _percent(change: Decimal, previous: Decimal) -> str | None:
    if previous == 0:
        return None
    return f"{(change / abs(previous) * 100).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)}"


def changes_since(
    month: CollectionMonth, current: Sequence[Charge], previous: Sequence[Charge]
) -> Changes:
    now = rupees_by(current, lambda charge: charge.vendor)
    before = rupees_by(previous, lambda charge: charge.vendor)
    vendors = [
        VendorChange(
            vendor=vendor,
            previous_inr=amount(before.get(vendor, ZERO)),
            current_inr=amount(now.get(vendor, ZERO)),
            change_inr=amount(now.get(vendor, ZERO) - before.get(vendor, ZERO)),
            change_percent=_percent(
                now.get(vendor, ZERO) - before.get(vendor, ZERO), before.get(vendor, ZERO)
            ),
        )
        for vendor in {*now, *before}
    ]
    vendors.sort(key=lambda change: (-abs(Decimal(change.change_inr)), change.vendor.casefold()))
    return Changes(month=str(month), previous_month=str(month_before(month)), vendors=vendors)


def months_in_range(
    present: Sequence[str], first: str | None, last: str | None
) -> list[CollectionMonth]:
    """The collection months present in the ledger within the range, oldest first.

    With no first month, only the latest six months up to the last are kept.
    """
    months = sorted(
        month
        for month in present
        if (first is None or month >= first) and (last is None or month <= last)
    )
    if first is None:
        months = months[-DEFAULT_MONTHS:]
    return [CollectionMonth.parse(month) for month in months]


def spend_of_nothing() -> Spend:
    """Spend when the ledger holds no collection month in the range."""
    return Spend(
        from_month=None,
        to_month=None,
        months=[],
        vendors=[],
        source_accounts=[],
        shared_charges=0,
        inr_total=amount(ZERO),
        without_rupees=WithoutRupees(charges=0, totals=[]),
        changes=None,
    )


def spend(ledger: Ledger, months: Sequence[CollectionMonth]) -> Spend:
    """Spend over the given collection months, which must be oldest first."""
    if not months:
        return spend_of_nothing()
    charges = charges_in(ledger, months)
    latest = months[-1]
    previous = charges_in(ledger, [month_before(latest)])
    return Spend(
        from_month=str(months[0]),
        to_month=str(latest),
        months=[
            MonthSpend(
                month=str(month),
                inr_total=amount(rupee_total([c for c in charges if c.month == str(month)])),
            )
            for month in months
        ],
        vendors=[
            VendorSpend(vendor=vendor, inr_total=amount(total))
            for vendor, total in rupees_by(charges, lambda charge: charge.vendor).items()
        ],
        source_accounts=[
            SourceAccountSpend(source_account=account, inr_total=amount(total))
            for account, total in rupees_by(
                charges, lambda charge: charge.first_source_account
            ).items()
        ],
        shared_charges=sum(1 for charge in charges if len(charge.source_accounts) > 1),
        inr_total=amount(rupee_total(charges)),
        without_rupees=totals_without_rupees(charges),
        changes=changes_since(latest, [c for c in charges if c.month == str(latest)], previous),
    )
