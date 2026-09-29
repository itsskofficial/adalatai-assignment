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


def reading_doubts(extraction: Extraction, email: Email) -> list[Doubt]:
    """Doubts about whether the document was read correctly.

    These are the ones a stronger model may clear by reading the document again.
    """
    doubts: list[Doubt] = []
    if extraction.confidence != "high":
        detail = f": {extraction.doubts}" if extraction.doubts else ""
        doubts.append(Doubt(None, f"the reader was unsure{detail}"))

    stated = totals_stated_in(email)
    if stated and abs(extraction.total) not in {abs(amount) for amount in stated}:
        said = ", ".join(f"{amount:.2f}" for amount in stated)
        doubts.append(
            Doubt("total", f"the email says {said} and the document says {extraction.total:.2f}")
        )

    if extraction.subtotal is not None and extraction.tax is not None:
        added = abs(extraction.subtotal) + abs(extraction.tax)
        if abs(added - abs(extraction.total)) > ROUNDING:
            doubts.append(
                Doubt(
                    "total",
                    f"subtotal {extraction.subtotal:.2f} and tax {extraction.tax:.2f} "
                    f"do not add up to {extraction.total:.2f}",
                )
            )
    return doubts


def history_doubts(extraction: Extraction, history: History, threshold: Decimal) -> list[Doubt]:
    """Doubts raised by what the vendor has billed before.

    A credit note is money returned, so it is not compared with the usual charge.
    """
    doubts: list[Doubt] = []
    if extraction.document_type == "credit_note":
        return doubts

    if history.usual_currency and extraction.currency != history.usual_currency:
        doubts.append(
            Doubt(
                "currency",
                f"the currency is {extraction.currency}; "
                f"{extraction.vendor} usually bills in {history.usual_currency}",
            )
        )
    elif history.usual_amount:
        change = (extraction.total - history.usual_amount) / history.usual_amount
        if abs(change) > threshold:
            direction = "above" if change > 0 else "below"
            doubts.append(
                Doubt(
                    "total",
                    f"the total is {abs(change):.0%} {direction} the usual "
                    f"{history.usual_amount:.2f} {extraction.currency} for {extraction.vendor}",
                )
            )

    if history.already_this_month:
        ordinal = {1: "second", 2: "third"}.get(history.already_this_month, "another")
        doubts.append(
            Doubt(
                "invoice_date",
                f"{ordinal} {extraction.document_type} from {extraction.vendor} this month",
            )
        )
    return doubts


def summary_of(doubts: Sequence[Doubt]) -> str:
    """The doubts as one reason, for the state of the email.

    A document held only because it came with a doubted one has no doubt of its own.
    """
    if not doubts:
        return "held with another document of the email"
    if len(doubts) == 1:
        return doubts[0].reason
    return f"{doubts[0].reason}, and {len(doubts) - 1} more"
