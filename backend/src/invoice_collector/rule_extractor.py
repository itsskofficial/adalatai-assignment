"""Extraction by rules, for when the model cannot be used.

It reads the text of the PDF and looks for a labelled total, a date and a vendor it is told
of: one on the expected vendor list in the ledger, or else one the email names, by its
sender or its classification. No vendor is known to the code itself.

It is deliberately strict: when any field is unclear it fails, so the document goes to
review instead of being filed with a guess. What it reads is marked as unsure, so even a
reading it is sure of is held for a person to confirm (ADR 0008).
"""

import io
import re
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal

from pypdf import PdfReader
from pypdf.errors import PyPdfError

from invoice_collector.domain import DocumentType, Extraction
from invoice_collector.extractor import NO_HINTS, ExtractionFailed, Hints

# Why a reading by rules is held. Shown on the Review screen with the document.
READ_BY_RULES = "read by rules, not by a model, so a person must confirm every field"

_SYMBOLS = {"$": "USD", "US$": "USD", "€": "EUR", "£": "GBP", "₹": "INR"}
_CODES = ("USD", "EUR", "GBP", "INR")
_MONEY = r"(US\$|[$€£₹]|USD|EUR|GBP|INR)?\s?(-?\d[\d,]*\.\d{2})\s?(USD|EUR|GBP|INR)?"
_TOTAL = re.compile(
    r"^\s*(?:grand\s+)?(?:total|amount)(?:\s+(?:due|paid|charged))?\b[^\d$€£₹\n]*" + _MONEY,
    re.I | re.M,
)
_DATE_LABEL = re.compile(
    r"(?:invoice date|date of issue|issued on|issued|paid on|receipt date|date)\s*:?\s*([^\n]+)",
    re.I,
)
_DATE_FORMATS = ("%Y-%m-%d", "%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y", "%d/%m/%Y")
_DATE_TEXT = re.compile(
    r"\d{4}-\d{2}-\d{2}|[A-Z][a-z]+ \d{1,2}, \d{4}|\d{1,2} [A-Z][a-z]+ \d{4}|\d{2}/\d{2}/\d{4}"
)


class RuleExtractor:
    def __init__(self, known_vendors: Sequence[str] = ()) -> None:
        # Names known before any document is read, looked for with the expected vendors.
        # A run gives none: its names come with each document. The eval gives the golden
        # set's, as it has no ledger.
        self._known_vendors = tuple(known_vendors)

    def extract(self, pdf: bytes, hints: Hints = NO_HINTS) -> Extraction:
        text = _text_of(pdf)
        total, currency = _total(text)
        return Extraction(
            document_type=_document_type(text),
            vendor=self._vendor(text, hints),
            invoice_date=_invoice_date(text),
            total=total,
            currency=currency,
            confidence="low",
            doubts=READ_BY_RULES,
            by="rules",
        )

    def _vendor(self, text: str, hints: Hints) -> str:
        """The one expected vendor the text names; failing that, the one name the email
        gives that the text also names. Two names found at the same step are not guessed
        between."""
        for names in (hints.expected_vendors + self._known_vendors, hints.named_by_email):
            found = _named_in(text, names)
            if len(found) == 1:
                return found[0]
            if len(found) > 1:
                raise ExtractionFailed(
                    f"rules could not identify the vendor: the document names {' and '.join(found)}"
                )
        raise ExtractionFailed("rules could not identify the vendor")


def _named_in(text: str, names: Sequence[str]) -> list[str]:
    """The names the text holds as words, each once whatever its capitals."""
    found: dict[str, str] = {}
    for name in names:
        if name.strip() and re.search(rf"(?<!\w){re.escape(name.strip())}(?!\w)", text, re.I):
            found.setdefault(name.strip().casefold(), name.strip())
    return list(found.values())


def _text_of(pdf: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(pdf))
        text = "\n".join(page.extract_text() for page in reader.pages)
    except (PyPdfError, ValueError, OSError) as error:
        raise ExtractionFailed("rules could not read the PDF") from error
    if not text.strip():
        raise ExtractionFailed("rules found no text in the PDF")
    return text


def _total(text: str) -> tuple[Decimal, str]:
    matches = [
        m for m in _TOTAL.finditer(text) if not m.group(0).lower().lstrip().startswith("sub")
    ]
    if not matches:
        raise ExtractionFailed("rules could not find a total")
    before, amount, after = matches[-1].groups()
    currency = after or _SYMBOLS.get(before or "", before)
    if currency not in _CODES:
        raise ExtractionFailed("rules could not find the currency")
    return Decimal(amount.replace(",", "")), currency


def _invoice_date(text: str) -> date:
    for label in _DATE_LABEL.finditer(text):
        found = _DATE_TEXT.search(label.group(1))
        if not found:
            continue
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(found.group(0), fmt).date()
            except ValueError:
                continue
    raise ExtractionFailed("rules could not find the invoice date")


def _document_type(text: str) -> DocumentType:
    if re.search(r"\bcredit (?:note|memo)\b", text, re.I):
        return "credit_note"
    if re.search(r"\breceipt\b", text, re.I) and not re.search(r"\binvoice\b", text, re.I):
        return "receipt"
    return "invoice"
