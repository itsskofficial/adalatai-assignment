"""What the Vendors screen shows and does: the vendor list, and the decisions made on it.

Every vendor on the list is expected, suggested or ignored. A run adds vendors that have
billed the company as suggestions; a person accepts them, ignores them, or keeps the list
by hand. Each change is recorded in the vendor history with who made it.
"""

import re
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Response
from fastapi import Path as PathParameter
from pydantic import BaseModel

from invoice_collector.api.vendor_history import VendorAction, VendorHistory
from invoice_collector.charge_history import Charge, charges_in
from invoice_collector.charges import summarise
from invoice_collector.domain import (
    BillingCycle,
    ExpectedVendor,
    GapKind,
    VendorStatus,
)
from invoice_collector.ledger import Ledger
from invoice_collector.reconciler import reconcile_month, vendor_key

MONTHS_BILLED_SHOWN = 6
CURRENCY_PATTERN = re.compile(r"^[A-Za-z]{3}$")


class VendorEntry(BaseModel):
    vendor: str
    status: VendorStatus
    source_account: str | None
    billing_cycle: BillingCycle
    renewal_month: int | None
    usual_amount: str | None
    currency: str | None


class VendorView(VendorEntry):
    months_billed: list[str]
    latest_amount: str | None
    latest_currency: str | None
    gap: GapKind | None


class VendorList(BaseModel):
    latest_month: str | None
    expected: list[VendorView]
    suggested: list[VendorView]
    ignored: list[VendorView]


class VendorFields(BaseModel):
    """What a person may set on an entry. A field left out keeps its current value.

    The types are loose so that a wrong value is refused with a plain message.
    """

    vendor: str | None = None
    source_account: str | None = None
    billing_cycle: str | None = None
    renewal_month: int | str | None = None
    usual_amount: str | int | float | None = None
    currency: str | None = None


class VendorChange(BaseModel):
    vendor: str
    action: VendorAction
    person: str
    changed_at: str
    before: dict[str, Any] | None
    after: dict[str, Any] | None


class VendorRefused(Exception):
    """A vendor list entry breaks a rule. The message says which, in plain words."""


def _entry(vendor: ExpectedVendor) -> VendorEntry:
    return VendorEntry(
        vendor=vendor.vendor,
        status=vendor.status,
        source_account=vendor.source_account,
        billing_cycle=vendor.billing_cycle,
        renewal_month=vendor.renewal_month,
        usual_amount=str(vendor.usual_amount) if vendor.usual_amount is not None else None,
        currency=vendor.currency,
    )


def _blank_to_none(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    return value.strip()


def _renewal_month(value: int | str | None) -> int | None | str:
    """The renewal month as a number, None when blank, or the text when it is not a number."""
    if isinstance(value, str):
        if not value.strip():
            return None
        try:
            return int(value.strip())
        except ValueError:
            return value
    return value


def _usual_amount(value: str | int | float | None) -> Decimal | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        amount = Decimal(str(value).strip())
    except InvalidOperation:
        raise VendorRefused("The usual amount must be a positive number") from None
    if not amount.is_finite() or amount <= 0:
        raise VendorRefused("The usual amount must be a positive number")
    return amount


def with_changes(current: ExpectedVendor, fields: VendorFields) -> ExpectedVendor:
    """The entry with the given fields applied, or VendorRefused if it breaks a rule."""
    given = fields.model_fields_set

    name = (fields.vendor if "vendor" in given else current.vendor) or ""
    if not name.strip():
        raise VendorRefused("The vendor name must not be blank")

    cycle = fields.billing_cycle if "billing_cycle" in given else current.billing_cycle
    if cycle not in ("monthly", "annual"):
        raise VendorRefused("The billing cycle must be monthly or annual")

    renewal = _renewal_month(
        fields.renewal_month if "renewal_month" in given else current.renewal_month
    )
    if cycle == "annual" and not (isinstance(renewal, int) and 1 <= renewal <= 12):
        raise VendorRefused("An annual vendor needs a renewal month from 1 to 12")
    if cycle == "monthly" and renewal is not None:
        raise VendorRefused("A monthly vendor has no renewal month")

    usual = _usual_amount(fields.usual_amount) if "usual_amount" in given else current.usual_amount

    currency = _blank_to_none(fields.currency if "currency" in given else current.currency)
    if currency is not None and not CURRENCY_PATTERN.match(currency):
        raise VendorRefused("The currency must be a three-letter code, such as USD")

    source_account = (
        _blank_to_none(fields.source_account)
        if "source_account" in given
        else current.source_account
    )
    return ExpectedVendor(
        vendor=name.strip(),
        source_account=source_account,
        billing_cycle=cycle,
        renewal_month=renewal if isinstance(renewal, int) else None,
        usual_amount=usual,
        currency=currency.upper() if currency is not None else None,
        status=current.status,
    )


def _latest(charges: Sequence[Charge]) -> Charge | None:
    return max(charges, key=lambda c: (c.invoice_date, c.month), default=None)


def vendor_list(ledger: Ledger) -> VendorList:
    """Every vendor on the list, with what the ledger says about its billing."""
    months = ledger.months()
    billed: dict[str, list[Charge]] = {}
    # Money returned is not a month billed, nor the amount a vendor usually bills.
    for charge in charges_in(ledger, months):
        if charge.document_type != "credit_note":
            billed.setdefault(vendor_key(charge.vendor), []).append(charge)

    gaps: dict[str, GapKind] = {}
    if months:
        latest_month = months[-1]
        reconciliation = reconcile_month(
            ledger, latest_month, summarise(ledger.documents(latest_month))
        )
        gaps = {gap.vendor: gap.kind for gap in reconciliation.gaps}

    grouped: dict[VendorStatus, list[VendorView]] = {"expected": [], "suggested": [], "ignored": []}
    for vendor in ledger.expected_vendors():
        charges = billed.get(vendor_key(vendor.vendor), [])
        latest = _latest(charges)
        months_billed = sorted({charge.month for charge in charges})[-MONTHS_BILLED_SHOWN:]
        grouped[vendor.status].append(
            VendorView(
                **_entry(vendor).model_dump(),
                months_billed=months_billed,
                latest_amount=f"{latest.total:.2f}" if latest is not None else None,
                latest_currency=latest.currency if latest is not None else None,
                gap=gaps.get(vendor.vendor) if vendor.status == "expected" else None,
            )
        )
    return VendorList(
        latest_month=str(months[-1]) if months else None,
        expected=grouped["expected"],
        suggested=grouped["suggested"],
        ignored=grouped["ignored"],
    )


def _find(vendors: Sequence[ExpectedVendor], name: str) -> ExpectedVendor:
    for vendor in vendors:
        if vendor.vendor == name:
            return vendor
    raise HTTPException(status_code=404, detail=f"{name} is not on the vendor list")


def _refuse_duplicate(vendors: Sequence[ExpectedVendor], name: str, editing: str | None) -> None:
    """Refuses a name that is another spelling of a vendor already on the list."""
    for vendor in vendors:
        if vendor.vendor != editing and vendor_key(vendor.vendor) == vendor_key(name):
            raise HTTPException(
                status_code=409, detail=f"{vendor.vendor} is already on the vendor list"
            )


def _applied(current: ExpectedVendor, fields: VendorFields | None) -> ExpectedVendor:
    try:
        return with_changes(current, fields or VendorFields())
    except VendorRefused as refused:
        raise HTTPException(status_code=422, detail=str(refused)) from None


BLANK = ExpectedVendor(
    vendor="",
    source_account=None,
    billing_cycle="monthly",
    renewal_month=None,
    usual_amount=None,
    currency=None,
    status="expected",
)

LedgerFactory = Callable[[], Ledger]
PersonDependency = Callable[..., str]
Fields = Annotated[VendorFields | None, Body()]
VendorPath = Annotated[str, PathParameter(description="A vendor's name, which may hold slashes")]


def vendor_routes(
    ledger_factory: LedgerFactory,
    history: VendorHistory,
    signed_in_person: PersonDependency,
    now: Callable[[], datetime],
) -> APIRouter:
    """The Vendors screen's routes. Vendor names in paths may hold slashes and spaces."""
    router = APIRouter(prefix="/vendors")

    @contextmanager
    def opened() -> Generator[Ledger]:
        # Opened inside each route, since SQLite keeps a connection to its own thread.
        ledger = ledger_factory()
        try:
            yield ledger
        finally:
            ledger.close()

    def save(
        ledger: Ledger,
        action: VendorAction,
        person: str,
        before: ExpectedVendor | None,
        after: ExpectedVendor,
    ) -> VendorEntry:
        ledger.save_expected_vendor(after)
        if before is not None and before.vendor != after.vendor:
            ledger.remove_expected_vendor(before.vendor)
        history.record(action, person, now(), before, after)
        return _entry(after)

    def decide(
        name: str,
        person: str,
        *,
        status_before: VendorStatus,
        status_after: VendorStatus,
        action: VendorAction,
        fields: VendorFields | None = None,
    ) -> VendorEntry:
        with opened() as ledger:
            vendors = ledger.expected_vendors()
            current = _find(vendors, name)
            if current.status != status_before:
                article = "an" if status_before[0] in "aeiou" else "a"
                raise HTTPException(
                    status_code=409, detail=f"{name} is not {article} {status_before} vendor"
                )
            # Taken as it stands, an entry is kept as the run suggested it, unchecked.
            kept = current if fields is None else _applied(current, fields)
            changed = replace(kept, status=status_after)
            _refuse_duplicate(vendors, changed.vendor, editing=current.vendor)
            return save(ledger, action, person, current, changed)

    @router.get("")
    def vendors() -> VendorList:  # pyright: ignore[reportUnusedFunction]
        with opened() as ledger:
            return vendor_list(ledger)

    @router.post("", status_code=201)
    def add(  # pyright: ignore[reportUnusedFunction]
        person: Annotated[str, Depends(signed_in_person)],
        fields: Fields = None,
    ) -> VendorEntry:
        added = _applied(BLANK, fields)
        with opened() as ledger:
            _refuse_duplicate(ledger.expected_vendors(), added.vendor, editing=None)
            return save(ledger, "added", person, None, added)

    @router.put("/{vendor:path}")
    def edit(  # pyright: ignore[reportUnusedFunction]
        vendor: VendorPath,
        person: Annotated[str, Depends(signed_in_person)],
        fields: Fields = None,
    ) -> VendorEntry:
        with opened() as ledger:
            vendors = ledger.expected_vendors()
            current = _find(vendors, vendor)
            edited = _applied(current, fields)
            _refuse_duplicate(vendors, edited.vendor, editing=current.vendor)
            return save(ledger, "edited", person, current, edited)

    @router.delete("/{vendor:path}", status_code=204)
    def remove(  # pyright: ignore[reportUnusedFunction]
        vendor: VendorPath,
        person: Annotated[str, Depends(signed_in_person)],
    ) -> Response:
        with opened() as ledger:
            current = _find(ledger.expected_vendors(), vendor)
            ledger.remove_expected_vendor(current.vendor)
        history.record("removed", person, now(), current, None)
        return Response(status_code=204)

    @router.post("/{vendor:path}/accept")
    def accept(  # pyright: ignore[reportUnusedFunction]
        vendor: VendorPath,
        person: Annotated[str, Depends(signed_in_person)],
        fields: Fields = None,
    ) -> VendorEntry:
        return decide(
            vendor,
            person,
            status_before="suggested",
            status_after="expected",
            action="accepted",
            fields=fields,
        )

    @router.post("/{vendor:path}/ignore")
    def ignore(  # pyright: ignore[reportUnusedFunction]
        vendor: VendorPath,
        person: Annotated[str, Depends(signed_in_person)],
    ) -> VendorEntry:
        return decide(
            vendor, person, status_before="suggested", status_after="ignored", action="ignored"
        )

    @router.post("/{vendor:path}/restore")
    def restore(  # pyright: ignore[reportUnusedFunction]
        vendor: VendorPath,
        person: Annotated[str, Depends(signed_in_person)],
    ) -> VendorEntry:
        return decide(
            vendor, person, status_before="ignored", status_after="suggested", action="restored"
        )

    @router.get("/{vendor:path}/history")
    def changes(vendor: VendorPath) -> list[VendorChange]:  # pyright: ignore[reportUnusedFunction]
        return [
            VendorChange(
                vendor=change.vendor,
                action=change.action,
                person=change.person,
                changed_at=change.changed_at.isoformat(),
                before=change.before,
                after=change.after,
            )
            for change in history.of(vendor)
        ]

    return router
