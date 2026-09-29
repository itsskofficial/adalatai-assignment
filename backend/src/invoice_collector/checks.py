"""Decides whether what was read from a billing document can be trusted.

A model's own rating of its confidence is not enough: in the first eval it rated itself
high on every answer, right or wrong. So what it read is checked against the document's
own arithmetic, against the email the document came in, and against what the vendor
has billed before. Anything that fails a check is a doubt, and a document with a doubt
is held for a person to confirm.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from invoice_collector.classifier import text_of
from invoice_collector.domain import Doubt, Email, Extraction

# One unit of the smallest coin, to allow for rounding of tax.
ROUNDING = Decimal("0.01")

_LABELLED_TOTAL = re.compile(
    r"(?:grand\s+total|total(?:\s+(?:due|paid|charged|amount))?|amount\s+(?:due|paid|charged))"
    r"[^\d\n$€£₹-]{0,40}(?:US\$|[$€£₹]|USD|EUR|GBP|INR)?\s?(-?\d[\d,]*\.\d{2})",
    re.I,
)
_NOT_A_TOTAL = re.compile(r"sub\s*-?\s*total", re.I)


@dataclass(frozen=True)
class History:
    """What is known of a vendor from the expected vendor list and from earlier months."""

    usual_amount: Decimal | None = None
    usual_currency: str | None = None
    # Billing documents of the same type already collected from the vendor this month.
    already_this_month: int = 0


def totals_stated_in(email: Email) -> list[Decimal]:
    """Amounts the email itself calls a total."""
    text = _NOT_A_TOTAL.sub("subtotal-line", text_of(email))
    return [Decimal(amount.replace(",", "")) for amount in _LABELLED_TOTAL.findall(text)]


@dataclass(frozen=True)
class CheckResult:
    """One check that ran on what was read, and the doubts it raised. With none, it passed."""

    check: str
    doubts: tuple[Doubt, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.doubts


def _doubts_of(results: Sequence[CheckResult]) -> list[Doubt]:
    return [doubt for result in results for doubt in result.doubts]


def reading_checks(extraction: Extraction, email: Email) -> list[CheckResult]:
    """The checks of whether the document was read correctly, each with its result.

    A check that had nothing to compare, such as a total when the email states none, did
    not run and is left out.
    """
    results: list[CheckResult] = []
    unsure: tuple[Doubt, ...] = ()
    if extraction.confidence != "high":
        detail = f": {extraction.doubts}" if extraction.doubts else ""
        unsure = (Doubt(None, f"the reader was unsure{detail}"),)
    results.append(CheckResult("the reader's own confidence", unsure))

    stated = totals_stated_in(email)
    if stated:
        differs: tuple[Doubt, ...] = ()
        if abs(extraction.total) not in {abs(amount) for amount in stated}:
            said = ", ".join(f"{amount:.2f}" for amount in stated)
            differs = (
                Doubt(
                    "total", f"the email says {said} and the document says {extraction.total:.2f}"
                ),
            )
        results.append(CheckResult("the total the email states", differs))

    if extraction.subtotal is not None and extraction.tax is not None:
        added = abs(extraction.subtotal) + abs(extraction.tax)
        apart: tuple[Doubt, ...] = ()
        if abs(added - abs(extraction.total)) > ROUNDING:
            apart = (
                Doubt(
                    "total",
                    f"subtotal {extraction.subtotal:.2f} and tax {extraction.tax:.2f} "
                    f"do not add up to {extraction.total:.2f}",
                ),
            )
        results.append(CheckResult("subtotal and tax add up to the total", apart))
    return results


def reading_doubts(extraction: Extraction, email: Email) -> list[Doubt]:
    """Doubts about whether the document was read correctly.

    These are the ones a stronger model may clear by reading the document again.
    """
    return _doubts_of(reading_checks(extraction, email))


def history_checks(
    extraction: Extraction, history: History, threshold: Decimal
) -> list[CheckResult]:
    """The checks against what the vendor has billed before, each with its result.

    A credit note is money returned, so it is not compared with the usual charge, and
    none of these checks runs on it.
    """
    results: list[CheckResult] = []
    if extraction.document_type == "credit_note":
        return results

    if history.usual_currency:
        other: tuple[Doubt, ...] = ()
        if extraction.currency != history.usual_currency:
            other = (
                Doubt(
                    "currency",
                    f"the currency is {extraction.currency}; "
                    f"{extraction.vendor} usually bills in {history.usual_currency}",
                ),
            )
        results.append(CheckResult("the vendor's usual currency", other))
    if history.usual_amount and (
        not history.usual_currency or extraction.currency == history.usual_currency
    ):
        change = (extraction.total - history.usual_amount) / history.usual_amount
        far: tuple[Doubt, ...] = ()
        if abs(change) > threshold:
            direction = "above" if change > 0 else "below"
            far = (
                Doubt(
                    "total",
                    f"the total is {abs(change):.0%} {direction} the usual "
                    f"{history.usual_amount:.2f} {extraction.currency} for {extraction.vendor}",
                ),
            )
        results.append(CheckResult("the vendor's usual amount", far))

    again: tuple[Doubt, ...] = ()
    if history.already_this_month:
        ordinal = {1: "second", 2: "third"}.get(history.already_this_month, "another")
        again = (
            Doubt(
                "invoice_date",
                f"{ordinal} {extraction.document_type} from {extraction.vendor} this month",
            ),
        )
    results.append(CheckResult(f"one {extraction.document_type} from the vendor this month", again))
    return results


def history_doubts(extraction: Extraction, history: History, threshold: Decimal) -> list[Doubt]:
    """Doubts raised by what the vendor has billed before.

    A credit note is money returned, so it is not compared with the usual charge.
    """
    return _doubts_of(history_checks(extraction, history, threshold))


def summary_of(doubts: Sequence[Doubt]) -> str:
    """The doubts as one reason, for the state of the email.

    A document held only because it came with a doubted one has no doubt of its own.
    """
    if not doubts:
        return "held with another document of the email"
    if len(doubts) == 1:
        return doubts[0].reason
    return f"{doubts[0].reason}, and {len(doubts) - 1} more"
