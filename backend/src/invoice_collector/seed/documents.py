"""Billing documents of the fictional company, and how they look as HTML and text."""

import calendar
import random
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from html import escape

from invoice_collector.domain import DocumentType
from invoice_collector.seed.catalogue import (
    COMPANY,
    COMPANY_ADDRESS,
    DateStyle,
    Vendor,
    money,
)

_SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "INR": "₹"}
_TITLES = {"invoice": "Invoice", "receipt": "Receipt", "credit_note": "Credit note"}
_TOTAL_LABELS = {"invoice": "Total due", "receipt": "Total paid", "credit_note": "Total credited"}

STYLE = """<style>
  body { font-family: Helvetica, Arial, sans-serif; color: #1d1c1d; margin: 40px; }
  h1 { font-size: 22px; margin-bottom: 4px; }
  h2 { font-size: 17px; }
  .muted { color: #616061; font-size: 13px; }
  table { border-collapse: collapse; width: 100%; margin-top: 24px; }
  th, td { text-align: left; padding: 8px 6px; border-bottom: 1px solid #ddd; font-size: 13px; }
  td.num, th.num { text-align: right; white-space: nowrap; }
  .total td { font-weight: bold; border-top: 2px solid #1d1c1d; }
</style>"""


@dataclass(frozen=True)
class LineItem:
    description: str
    quantity: int
    unit_price: Decimal
    amount: Decimal


@dataclass(frozen=True)
class Document:
    vendor: Vendor
    document_type: DocumentType
    number: str
    invoice_date: date
    line_items: tuple[LineItem, ...]
    subtotal: Decimal
    tax: Decimal
    total: Decimal

    @property
    def currency(self) -> str:
        return self.vendor.currency

    @property
    def title(self) -> str:
        return _TITLES[self.document_type]


def document(
    vendor: Vendor,
    document_type: DocumentType,
    number: str,
    invoice_date: date,
    line_items: tuple[LineItem, ...],
) -> Document:
    subtotal = sum((item.amount for item in line_items), Decimal(0))
    tax = money(subtotal * vendor.tax_rate)
    return Document(
        vendor, document_type, number, invoice_date, line_items, subtotal, tax, subtotal + tax
    )


def line_items(
    vendor: Vendor, rng: random.Random, scale: Decimal = Decimal(1)
) -> tuple[LineItem, ...]:
    """What the vendor bills this time: the usual, with usage drifting a little."""
    items: list[LineItem] = []
    for line in vendor.lines:
        drift = Decimal(str(round(rng.uniform(0.93, 1.07), 4))) if line.usage else Decimal(1)
        quantity = max(1, round(line.quantity * drift * scale))
        items.append(
            LineItem(line.description, quantity, line.unit_price, money(quantity * line.unit_price))
        )
    return tuple(items)


def format_money(amount: Decimal, currency: str) -> str:
    sign = "-" if amount < 0 else ""
    return f"{sign}{_SYMBOLS[currency]}{abs(amount):,.2f}"


def format_unit_price(amount: Decimal, currency: str) -> str:
    """Metered prices are quoted to four places, the rest to two."""
    places = 4 if amount != money(amount) else 2
    sign = "-" if amount < 0 else ""
    return f"{sign}{_SYMBOLS[currency]}{abs(amount):,.{places}f}"


def format_money_plain(amount: Decimal, currency: str) -> str:
    return f"{currency} {amount:,.2f}"


def format_date(day: date, style: DateStyle) -> str:
    if style == "iso":
        return day.isoformat()
    if style == "short":
        return f"{day.day} {calendar.month_abbr[day.month]} {day.year}"
    return f"{calendar.month_name[day.month]} {day.day}, {day.year}"


def _rows(doc: Document) -> str:
    rows = [
        "<tr><th>Description</th><th class='num'>Qty</th>"
        "<th class='num'>Unit price</th><th class='num'>Amount</th></tr>"
    ]
    for item in doc.line_items:
        rows.append(
            f"<tr><td>{escape(item.description)}</td><td class='num'>{item.quantity}</td>"
            f"<td class='num'>{format_unit_price(item.unit_price, doc.currency)}</td>"
            f"<td class='num'>{format_money(item.amount, doc.currency)}</td></tr>"
        )
    rows.append(
        f"<tr><td>Subtotal</td><td></td><td></td>"
        f"<td class='num'>{format_money(doc.subtotal, doc.currency)}</td></tr>"
    )
    rows.append(
        f"<tr><td>{escape(doc.vendor.tax_label)}</td><td></td><td></td>"
        f"<td class='num'>{format_money(doc.tax, doc.currency)}</td></tr>"
    )
    rows.append(
        f"<tr class='total'><td>{_TOTAL_LABELS[doc.document_type]}</td><td></td><td></td>"
        f"<td class='num'>{format_money(doc.total, doc.currency)} {doc.currency}</td></tr>"
    )
    return "\n  ".join(rows)


def document_html(doc: Document) -> str:
    """The billing document as a page of its own: a PDF attachment or a portal page."""
    vendor = doc.vendor
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{doc.title} {escape(doc.number)}</title>
{STYLE}</head><body>
<h1>{escape(vendor.legal_name)}</h1>
<div class="muted">{escape(vendor.address)}</div>
<h2>{doc.title} {escape(doc.number)}</h2>
<p>{doc.title} date: {format_date(doc.invoice_date, vendor.date_style)}<br>
Bill to: {escape(COMPANY)}, {escape(COMPANY_ADDRESS)}</p>
<table>
  {_rows(doc)}
</table>
</body></html>
"""


def body_html(doc: Document) -> str:
    """The billing document written into the email itself."""
    vendor = doc.vendor
    return f"""<html><head><meta charset="utf-8">
{STYLE}</head><body>
<p>Hi Nyaya Labs,</p>
<p>Thanks for using {escape(vendor.name)}. This email is your {doc.title.lower()}.</p>
<h2>{doc.title} #{escape(doc.number)}</h2>
<p class="muted">{doc.title} date: {format_date(doc.invoice_date, vendor.date_style)}<br>
Billed to: {escape(COMPANY)}</p>
<table>
  {_rows(doc)}
</table>
<p class="muted">{escape(vendor.legal_name)}, {escape(vendor.address)}</p>
</body></html>
"""


def body_text(doc: Document) -> str:
    vendor = doc.vendor
    lines = [
        "Hi Nyaya Labs,",
        "",
        f"Thanks for using {vendor.name}. This email is your {doc.title.lower()}.",
        "",
        f"{doc.title} #{doc.number}",
        f"{doc.title} date: {format_date(doc.invoice_date, vendor.date_style)}",
        f"Billed to: {COMPANY}",
        "",
    ]
    lines += [
        f"{item.description} x {item.quantity}: {format_money_plain(item.amount, doc.currency)}"
        for item in doc.line_items
    ]
    lines += [
        f"Subtotal: {format_money_plain(doc.subtotal, doc.currency)}",
        f"{vendor.tax_label}: {format_money_plain(doc.tax, doc.currency)}",
        f"{_TOTAL_LABELS[doc.document_type]}: {format_money_plain(doc.total, doc.currency)}",
        "",
        f"{vendor.legal_name}, {vendor.address}",
    ]
    return "\n".join(lines) + "\n"


def paragraphs_html(paragraphs: tuple[str, ...]) -> str:
    body = "\n".join(f"<p>{escape(p)}</p>" for p in paragraphs)
    return f'<html><head><meta charset="utf-8"></head><body>\n{body}\n</body></html>\n'


def sign_in_html() -> str:
    """The page a login-gated portal link leads to. It holds no billing document."""
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Sign in</title>
{STYLE}</head><body>
<h1>Sign in</h1>
<p class="muted">Sign in to the billing account to view and download invoices.</p>
<form method="post" action="#">
  <p><label>Email <input type="email" name="email" autocomplete="username"></label></p>
  <p><label>Password <input type="password" name="password"
    autocomplete="current-password"></label></p>
  <p><button type="submit">Next</button></p>
</form>
</body></html>
"""
