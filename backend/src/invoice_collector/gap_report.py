"""Reads the expected vendor file, and writes and prints what a run found to be absent."""

import csv
import json
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from invoice_collector.domain import ExpectedVendor, Gap
from invoice_collector.run import RunResult
from invoice_collector.summary import as_text_cell

COLUMNS = ("vendor", "gap", "status", "source_account", "explanation")


class ExpectedVendorFileInvalid(Exception):
    pass


def _vendor(entry: dict[str, Any], path: Path) -> ExpectedVendor:
    try:
        cycle = entry.get("billing_cycle", "monthly")
        renewal_month = entry.get("renewal_month")
        source_account = entry.get("source_account")
        currency = entry.get("currency")
        if not isinstance(entry["vendor"], str) or not entry["vendor"].strip():
            raise ValueError("vendor must be a name")
        if cycle not in ("monthly", "annual"):
            raise ValueError(f"billing_cycle is {cycle!r}")
        # True is a number to Python, and is not a month.
        if renewal_month is not None and (
            type(renewal_month) is not int or renewal_month not in range(1, 13)
        ):
            raise ValueError("renewal_month must be a number from 1 to 12")
        if cycle == "annual" and renewal_month is None:
            raise ValueError("an annual vendor needs a renewal_month from 1 to 12")
        if source_account is not None and not isinstance(source_account, str):
            raise ValueError("source_account must be text")
        if currency is not None and not isinstance(currency, str):
            raise ValueError("currency must be text")
        usual = entry.get("usual_amount")
        return ExpectedVendor(
            vendor=entry["vendor"],
            source_account=source_account,
            billing_cycle=cycle,
            renewal_month=renewal_month,
            usual_amount=Decimal(str(usual)) if usual is not None else None,
            currency=currency,
        )
    except (KeyError, TypeError, ValueError, InvalidOperation) as error:
        raise ExpectedVendorFileInvalid(f"{path}: {entry!r} is not a vendor: {error}") from error


def read_expected_vendors(path: Path) -> list[ExpectedVendor]:
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ExpectedVendorFileInvalid(f"{path} could not be read: {error}") from error
    if not isinstance(entries, list):
        raise ExpectedVendorFileInvalid(f"{path} must hold a list of vendors")
    return [_vendor(entry, path) for entry in entries]  # pyright: ignore[reportUnknownArgumentType, reportUnknownVariableType]


def write_gaps(path: Path, gaps: Sequence[Gap]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(COLUMNS)
        for gap in gaps:
            writer.writerow(
                (
                    as_text_cell(gap.vendor),
                    gap.kind,
                    gap.status,
                    as_text_cell(gap.source_account or ""),
                    as_text_cell(gap.explanation or ""),
                )
            )


def lines(result: RunResult) -> list[str]:
    """What a run found to be absent, for printing."""
    found: list[str] = []
    for account, reason in sorted(result.failed_source_accounts.items()):
        found.append(f"Could not read {account}: {reason}")
    if result.gaps:
        found.append(f"Gaps: {len(result.gaps)}")
        for gap in result.gaps:
            detail = f" ({gap.explanation})" if gap.explanation else ""
            found.append(f"  {gap.kind}: {gap.vendor}{detail}")
    else:
        found.append("Gaps: none")
    for upcoming in result.upcoming:
        found.append(f"Upcoming: {upcoming.vendor}: {upcoming.note}")
    if result.suggested_vendors:
        names = ", ".join(v.vendor for v in result.suggested_vendors)
        found.append(f"Suggested vendors awaiting a decision: {names}")
    return found
