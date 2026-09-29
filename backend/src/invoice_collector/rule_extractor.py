"""Extraction by rules, for when the model cannot be used.

It reads the text of the PDF and looks for a labelled total, a date and a known vendor.
It is deliberately strict: when any field is unclear it fails, so the document goes to
review instead of being filed with a guess.
"""

import io
import re
from datetime import date, datetime
from decimal import Decimal

from pypdf import PdfReader
from pypdf.errors import PyPdfError

from invoice_collector.domain import DocumentType, Extraction
from invoice_collector.extractor import ExtractionFailed

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
    def __init__(self, known_vendors: tuple[str, ...]) -> None:
        self._known_vendors = known_vendors

    def extract(self, pdf: bytes) -> Extraction:
        text = _text_of(pdf)
        total, currency = _total(text)
        return Extraction(
            document_type=_document_type(text),
            vendor=self._vendor(text),
            invoice_date=_invoice_date(text),
            total=total,
            currency=currency,
            confidence="low",
            doubts="read by rules, not by the model",
            by="rules",
        )

    def _vendor(self, text: str) -> str:
        found = [v for v in self._known_vendors if re.search(rf"\b{re.escape(v)}\b", text, re.I)]
        if len(found) != 1:
            raise ExtractionFailed("rules could not identify the vendor")
        return found[0]


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
