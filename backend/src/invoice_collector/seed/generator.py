"""Generates the sample emails together with their correct answers.

Everything is derived from the catalogue and a seed, never from the clock, so two
runs describe the same mailboxes. The hard cases are written for the default
target month; the annual vendors in the catalogue renew around it.
"""

import calendar
import hashlib
import json
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path
from typing import Any, Protocol

from invoice_collector.domain import CollectionMonth, DocumentType
from invoice_collector.seed.catalogue import (
    DEFAULT_CURRENCY,
    DEFAULT_SOURCE_ACCOUNTS,
    OTHER_MAIL,
    VENDORS,
    Engineering,
    Finance,
    OtherMail,
    Vendor,
    money,
    vendor,
)
from invoice_collector.seed.documents import (
    Document,
    LineItem,
    body_html,
    body_text,
    document,
    document_html,
    format_date,
    format_money,
    line_items,
    paragraphs_html,
    sign_in_html,
)

# The hard cases of the target month, by vendor.
GAP = "Zoom"
ANOMALY = "Datadog"
ANOMALY_SCALE = Decimal("1.4")
ONE_MESSAGE_TO_TWO_ACCOUNTS = ("Slack", Engineering)
SAME_DOCUMENT_SENT_TWICE = ("AWS", Finance)
INVOICE_THEN_RECEIPT = ("Vercel", "Zoho")
CREDIT_NOTE = "Atlassian"
PAYMENT_RETRIED = "DocuSign"
MULTI_PAGE_FROM_LINES = 40
SIGN_IN_PAGE = "sign-in.html"

GoldenEntry = dict[str, Any]


class Renderer(Protocol):
    def render_html(self, html: str) -> bytes:
        """The HTML as a PDF."""
        ...


def _months_before(month: CollectionMonth, count: int) -> tuple[CollectionMonth, ...]:
    index = month.year * 12 + month.month - 1
    return tuple(
        CollectionMonth((index - n) // 12, (index - n) % 12 + 1) for n in range(count, 0, -1)
    )


@dataclass(frozen=True)
class SeedConfig:
    target_month: CollectionMonth = CollectionMonth(2026, 8)
    # The two months before the target month, unless given.
    history_months: tuple[CollectionMonth, ...] | None = None
    # In order: engineering, operations, finance.
    source_accounts: tuple[str, str, str] = DEFAULT_SOURCE_ACCOUNTS
    portal_base_url: str = "http://localhost:8765"
    seed: int = 14

    @property
    def history(self) -> tuple[CollectionMonth, ...]:
        if self.history_months is not None:
            return self.history_months
        return _months_before(self.target_month, 2)


@dataclass(frozen=True)
class SeedMessage:
    source_account: str
    file_name: str
    message_id: str
    raw: bytes
    golden: Mapping[str, Any]


@dataclass(frozen=True)
class SeedData:
    messages: tuple[SeedMessage, ...]
    portal_pages: Mapping[str, str]
    expected_vendors: Sequence[Mapping[str, Any]]
    answers: Mapping[str, Mapping[str, str]]

    @property
    def golden(self) -> list[Mapping[str, Any]]:
        return [m.golden for m in self.messages]


def _next_month(month: CollectionMonth) -> CollectionMonth:
    following = month.end
    return CollectionMonth(following.year, following.month)


def _day_in(month: CollectionMonth, day: int) -> date:
    last = calendar.monthrange(month.year, month.month)[1]
    return date(month.year, month.month, min(day, last))


def _month_name(month: CollectionMonth) -> str:
    return f"{calendar.month_name[month.month]} {month.year}"


@dataclass
class _Generator:
    config: SeedConfig
    renderer: Renderer
    messages: list[tuple[datetime, SeedMessage]] = field(default_factory=lambda: [])
    portal_pages: dict[str, str] = field(default_factory=lambda: {})
    answers: dict[str, dict[str, str]] = field(default_factory=lambda: {})
    _pdfs: dict[str, bytes] = field(default_factory=lambda: {})
    _file_names: set[tuple[str, str]] = field(default_factory=lambda: set())

    # Repeatable choices

    def _rng(self, key: str) -> random.Random:
        return random.Random(f"{self.config.seed}|{key}")

    def _token(self, key: str, length: int = 24) -> str:
        return hashlib.sha256(f"{self.config.seed}|{key}".encode()).hexdigest()[:length]

    def _time(self, day: date, key: str, *, early: bool = False) -> datetime:
        rng = self._rng(f"time|{key}")
        hour = rng.randrange(0, 3) if early else rng.randrange(3, 22)
        return datetime(
            day.year, day.month, day.day, hour, rng.randrange(60), rng.randrange(60), tzinfo=UTC
        )

    def _number(self, v: Vendor, key: str) -> str:
        return f"{v.number_prefix}-{self._rng(f'number|{key}').randrange(10**6, 10**7)}"

    def _pdf(self, html: str) -> bytes:
        # One rendering per document, so copies of a document are the same bytes.
        if html not in self._pdfs:
            self._pdfs[html] = self.renderer.render_html(html)
        return self._pdfs[html]

    # Emails

    def _add(
        self,
        *,
        key: str,
        month: CollectionMonth,
        accounts: Sequence[int],
        sender_name: str,
        sender_address: str,
        subject: str,
        when: datetime,
        text: str,
        html: str,
        name: str,
        golden: GoldenEntry,
        attachments: Sequence[tuple[str, bytes]] = (),
    ) -> None:
        """One message, delivered to each of the accounts it is addressed to."""
        domain = sender_address.rsplit("@", 1)[1]
        message_id = f"{self._token(f'message|{key}')}@{domain}"
        recipients = [self.config.source_accounts[a] for a in accounts]

        message = EmailMessage()
        user, _, sender_domain = sender_address.partition("@")
        message["From"] = Address(sender_name, user, sender_domain)
        message["To"] = recipients[0]
        if recipients[1:]:
            message["Cc"] = ", ".join(recipients[1:])
        message["Subject"] = subject
        message["Date"] = format_datetime(when)
        message["Message-ID"] = f"<{message_id}>"
        message.set_content(text, cte="quoted-printable")
        message.add_alternative(html, subtype="html", cte="quoted-printable")
        for filename, content in attachments:
            message.add_attachment(
                content, maintype="application", subtype="pdf", filename=filename
            )
        multiparts = [part for part in message.walk() if part.is_multipart()]
        for index, part in enumerate(multiparts):
            part.set_boundary(f"=_seed_{self._token(f'boundary|{key}', 16)}_{index}")
        raw = message.as_bytes()

        for recipient in recipients:
            file_name = f"{when:%Y-%m-%d}_{name}.eml"
            suffix = 2
            while (recipient, file_name) in self._file_names:
                file_name = f"{when:%Y-%m-%d}_{name}_{suffix}.eml"
                suffix += 1
            self._file_names.add((recipient, file_name))
            entry: GoldenEntry = {
                "source_account": recipient,
                "message_id": message_id,
                "file_name": file_name,
                "month": str(month),
                **golden,
            }
            self.messages.append((when, SeedMessage(recipient, file_name, message_id, raw, entry)))

    def _billing_document(
        self,
        doc: Document,
        month: CollectionMonth,
        *,
        key: str,
        charge: str,
        accounts: Sequence[int] | None = None,
        when: datetime | None = None,
        subject: str | None = None,
        labels: Sequence[str] = (),
    ) -> None:
        v = doc.vendor
        title = doc.title.lower()
        found: list[str] = list(labels)
        portal_url: str | None = None
        attachments: list[tuple[str, bytes]] = []

        if when is None:
            if v.emailed_next_day:
                when = self._time(doc.invoice_date + timedelta(days=1), key, early=True)
            else:
                when = self._time(doc.invoice_date, key)

        if v.invoice_format == "attachment":
            pdf = self._pdf(document_html(doc))
            attachments.append((f"{doc.title.replace(' ', '')}-{doc.number}.pdf", pdf))
            self.answers[hashlib.sha256(pdf).hexdigest()] = {
                "document_type": doc.document_type,
                "vendor": v.name,
                "invoice_date": doc.invoice_date.isoformat(),
                "total": str(doc.total),
                "currency": doc.currency,
            }
            paragraphs = (
                "Hi Nyaya Labs,",
                f"Your {v.name} {title} {doc.number} is attached to this email as a PDF.",
                f"Thanks,\nThe {v.name} team",
            )
            text = "\n\n".join(paragraphs) + "\n"
            html = paragraphs_html(paragraphs)
        elif v.invoice_format == "body":
            text, html = body_text(doc), body_html(doc)
        else:
            base = self.config.portal_base_url.rstrip("/")
            if v.portal == "login_gated":
                page = SIGN_IN_PAGE
                self.portal_pages[page] = sign_in_html()
                ask = "Sign in to your billing account to view and download it"
                found.append("login_gated_portal_link")
            else:
                page = f"in_{self._token(f'portal|{key}')}.html"
                self.portal_pages[page] = document_html(doc)
                ask = "You can view and download it here"
                found.append("tokenised_portal_link")
            portal_url = f"{base}/{page}"
            opening = f"Your {v.name} {title} for {_month_name(month)} is ready."
            text = f"Hi Nyaya Labs,\n\n{opening}\n\n{ask}: {portal_url}\n\nThe {v.name} team\n"
            html = (
                '<html><head><meta charset="utf-8"></head><body>\n'
                f"<p>Hi Nyaya Labs,</p>\n<p>{opening}</p>\n"
                f'<p>{ask}: <a href="{portal_url}">View {title}</a></p>\n'
                f"<p>The {v.name} team</p>\n</body></html>\n"
            )

        if doc.currency != DEFAULT_CURRENCY:
            found.append("foreign_currency")
        if len(doc.line_items) >= MULTI_PAGE_FROM_LINES:
            found.append("multi_page")
        if doc.document_type == "credit_note":
            found.append("credit_note")
        if (when.year, when.month) != (doc.invoice_date.year, doc.invoice_date.month):
            found.append("late_arrival")
        if not v.expected:
            found.append("suggested_vendor")

        self._add(
            key=key,
            month=month,
            accounts=accounts if accounts is not None else (v.account,),
            sender_name=v.sender_name,
            sender_address=v.sender_address,
            subject=subject or v.subject.format(number=doc.number),
            when=when,
            text=text,
            html=html,
            name=f"{v.slug}_{doc.document_type.replace('_', '-')}",
            attachments=attachments,
            golden={
                "kind": doc.document_type,
                "invoice_format": v.invoice_format,
                "vendor": v.name,
                "invoice_date": doc.invoice_date.isoformat(),
                "total": str(doc.total),
                "currency": doc.currency,
                "document_type": doc.document_type,
                "invoice_number": doc.number,
                "subtotal": str(doc.subtotal),
                "tax": str(doc.tax),
                "line_items": [
                    {
                        "description": item.description,
                        "quantity": item.quantity,
                        "unit_price": str(item.unit_price),
                        "amount": str(item.amount),
                    }
                    for item in doc.line_items
                ],
                "charge": charge,
                "portal_url": portal_url,
                "labels": sorted(set(found)),
            },
        )

    def _without_document(
        self,
        *,
        key: str,
        month: CollectionMonth,
        kind: str,
        account: int,
        sender_name: str,
        sender_address: str,
        subject: str,
        day: date,
        paragraphs: tuple[str, ...],
        name: str,
        vendor_name: str | None,
        labels: Sequence[str],
    ) -> None:
        """A billing signal, or an email that has nothing to do with billing."""
        self._add(
            key=key,
            month=month,
            accounts=(account,),
            sender_name=sender_name,
            sender_address=sender_address,
            subject=subject,
            when=self._time(day, key),
            text="\n\n".join(paragraphs) + "\n",
            html=paragraphs_html(paragraphs),
            name=name,
            golden={
                "kind": kind,
                "invoice_format": None,
                "vendor": vendor_name,
                "invoice_date": None,
                "total": None,
                "currency": None,
                "document_type": None,
                "invoice_number": None,
                "subtotal": None,
                "tax": None,
                "line_items": [],
                "charge": None,
                "portal_url": None,
                "labels": sorted(set(labels)),
            },
        )

    def _signal(
        self,
        v: Vendor,
        month: CollectionMonth,
        kind: str,
        day: date,
        subject: str,
        paragraphs: tuple[str, ...],
        *,
        key: str,
        labels: Sequence[str] = (),
    ) -> None:
        self._without_document(
            key=f"{month}|{v.name}|{kind}|{key}",
            month=month,
            kind=kind,
            account=v.account,
            sender_name=v.sender_name,
            sender_address=v.sender_address,
            subject=subject,
            day=day,
            paragraphs=paragraphs,
            name=f"{v.slug}_{kind.replace('_', '-')}",
            vendor_name=v.name,
            labels=(kind, *labels),
        )

    def _other(self, mail: OtherMail, month: CollectionMonth) -> None:
        self._without_document(
            key=f"{month}|{mail.key}",
            month=month,
            kind="not_billing",
            account=mail.account,
            sender_name=mail.sender_name,
            sender_address=mail.sender_address,
            subject=mail.subject.format(month_name=calendar.month_name[month.month]),
            day=_day_in(month, mail.day),
            paragraphs=mail.paragraphs,
            name=mail.key,
            vendor_name=None,
            labels=(mail.label,),
        )

    # Months

    def _document(
        self,
        v: Vendor,
        month: CollectionMonth,
        *,
        document_type: DocumentType | None = None,
        day: int | None = None,
        scale: Decimal = Decimal(1),
        number_key: str = "",
    ) -> Document:
        kind = document_type or v.document_type
        return document(
            v,
            kind,
            self._number(v, f"{month}|{v.name}|{kind}|{number_key}"),
            _day_in(month, day or v.billing_day),
            line_items(v, self._rng(f"lines|{month}|{v.name}"), scale),
        )

    def _usual(self, v: Vendor, month: CollectionMonth) -> None:
        doc = self._document(v, month)
        self._billing_document(doc, month, key=f"{month}|{v.name}", charge=f"{month}_{v.slug}")

    def history_month(self, month: CollectionMonth, position: int) -> None:
        for v in VENDORS:
            if v.billing_cycle == "annual" and v.renewal_month != month.month:
                continue
            self._usual(v, month)
        for mail in OTHER_MAIL:
            if position in mail.history:
                self._other(mail, month)

    def target_month(self, month: CollectionMonth) -> None:
        for v in VENDORS:
            if not v.expected:
                continue
            if v.billing_cycle == "annual":
                if v.renewal_month == month.month:
                    self._usual(v, month)
                elif v.renewal_month == _next_month(month).month:
                    self._renewal_reminder(v, month)
                continue
            key, charge = f"{month}|{v.name}", f"{month}_{v.slug}"
            if v.name == GAP:
                self._payment_failed_twice(v, month)
            elif v.name == ANOMALY:
                doc = self._document(v, month, scale=ANOMALY_SCALE)
                self._billing_document(doc, month, key=key, charge=charge, labels=["anomaly"])
            elif v.name == ONE_MESSAGE_TO_TWO_ACCOUNTS[0]:
                self._billing_document(
                    self._document(v, month),
                    month,
                    key=key,
                    charge=charge,
                    accounts=(v.account, ONE_MESSAGE_TO_TWO_ACCOUNTS[1]),
                    labels=["duplicate_across_accounts"],
                )
            elif v.name == SAME_DOCUMENT_SENT_TWICE[0]:
                doc = self._document(v, month)
                for account in (v.account, SAME_DOCUMENT_SENT_TWICE[1]):
                    self._billing_document(
                        doc,
                        month,
                        key=f"{key}|{account}",
                        charge=charge,
                        accounts=(account,),
                        labels=["duplicate_across_accounts"],
                    )
            elif v.name in INVOICE_THEN_RECEIPT:
                self._invoice_then_receipt(v, month)
            elif v.name == PAYMENT_RETRIED:
                self._payment_retried(v, month)
            else:
                self._usual(v, month)
            if v.name == CREDIT_NOTE:
                self._credit_note(v, month)
        for mail in OTHER_MAIL:
            self._other(mail, month)

    # Hard cases

    def _invoice_then_receipt(self, v: Vendor, month: CollectionMonth) -> None:
        charge = f"{month}_{v.slug}"
        invoice = self._document(v, month, document_type="invoice")
        self._billing_document(
            invoice, month, key=f"{month}|{v.name}", charge=charge, labels=["invoice_and_receipt"]
        )
        paid = document(
            v,
            "receipt",
            self._number(v, f"{month}|{v.name}|receipt"),
            invoice.invoice_date + timedelta(days=1),
            invoice.line_items,
        )
        self._billing_document(
            paid,
            month,
            key=f"{month}|{v.name}|receipt",
            charge=charge,
            subject=f"Receipt for your {v.name} payment, invoice {invoice.number}",
            labels=["invoice_and_receipt"],
        )

    def _credit_note(self, v: Vendor, month: CollectionMonth) -> None:
        removed = v.lines[-1]
        users = 5
        item = LineItem(
            f"Credit for removed users: {removed.description}",
            users,
            -removed.unit_price,
            money(-removed.unit_price * users),
        )
        doc = document(
            v,
            "credit_note",
            self._number(v, f"{month}|{v.name}|credit"),
            _day_in(month, v.billing_day + 7),
            (item,),
        )
        self._billing_document(
            doc,
            month,
            key=f"{month}|{v.name}|credit",
            charge=f"{month}_{v.slug}_credit",
            subject=f"Credit note {doc.number} from {v.name}",
        )

    def _payment_failed_twice(self, v: Vendor, month: CollectionMonth) -> None:
        amount = format_money(v.usual_amount, v.currency)
        plan = v.lines[0].description
        first = _day_in(month, v.billing_day)
        self._signal(
            v,
            month,
            "payment_failed",
            first,
            f"Action required: your {v.name} payment failed",
            (
                "Hi Nyaya Labs,",
                f"Your payment of {amount} for {plan} failed because the card ending "
                "4242 was declined. No money has been taken.",
                "We will try the card again in three days. To avoid an interruption, "
                "update your payment method in the billing settings.",
            ),
            key="first",
            labels=["gap"],
        )
        self._signal(
            v,
            month,
            "payment_failed",
            first + timedelta(days=3),
            f"Second notice: your {v.name} payment failed again",
            (
                "Hi Nyaya Labs,",
                f"We tried the card ending 4242 again and the payment of {amount} failed. "
                "No invoice has been issued for this period.",
                "Your licences stay active for 14 days. Update your payment method to "
                "keep your plan.",
            ),
            key="second",
            labels=["gap"],
        )

    def _payment_retried(self, v: Vendor, month: CollectionMonth) -> None:
        """The first attempt fails and the second succeeds, so there is no gap."""
        failed = _day_in(month, v.billing_day)
        doc = self._document(v, month, day=failed.day + 2)
        self._signal(
            v,
            month,
            "payment_failed",
            failed,
            f"Your {v.name} payment failed",
            (
                "Hi Nyaya Labs,",
                f"Your payment of {format_money(doc.total, doc.currency)} failed because "
                "your bank declined the card ending 4242.",
                "We will try again in two days. You do not need to do anything if the "
                "card details are correct.",
            ),
            key="retried",
        )
        self._billing_document(doc, month, key=f"{month}|{v.name}", charge=f"{month}_{v.slug}")

    def _renewal_reminder(self, v: Vendor, month: CollectionMonth) -> None:
        renews = _day_in(_next_month(month), v.billing_day)
        self._signal(
            v,
            month,
            "renewal_reminder",
            renews - timedelta(days=30),
            f"Your {v.name} subscription renews on {format_date(renews, 'long')}",
            (
                "Hi Nyaya Labs,",
                f"Your annual subscription, {v.lines[0].description}, renews on "
                f"{format_date(renews, 'long')}.",
                f"On that date we will charge {format_money(v.usual_amount, v.currency)} "
                "to the card on file and email your invoice. No money has been taken yet "
                "and you do not need to do anything.",
            ),
            key="reminder",
        )


def _expected_vendors(config: SeedConfig) -> list[dict[str, Any]]:
    return [
        {
            "vendor": v.name,
            "source_account": config.source_accounts[v.account],
            "billing_cycle": v.billing_cycle,
            "renewal_month": v.renewal_month,
            "usual_amount": str(v.usual_amount),
            "currency": v.currency,
        }
        for v in VENDORS
        if v.expected
    ]


def generate(config: SeedConfig, renderer: Renderer) -> SeedData:
    """The sample emails of three source accounts, each with its correct answer."""
    # Fail early on a catalogue that no longer holds the vendors the hard cases name.
    for name in (GAP, ANOMALY, CREDIT_NOTE, PAYMENT_RETRIED, *INVOICE_THEN_RECEIPT):
        vendor(name)

    generator = _Generator(config, renderer)
    for position, month in enumerate(config.history):
        generator.history_month(month, position)
    generator.target_month(config.target_month)

    order = {account: n for n, account in enumerate(config.source_accounts)}
    messages = sorted(
        generator.messages,
        key=lambda m: (order[m[1].source_account], m[0], m[1].file_name),
    )
    return SeedData(
        messages=tuple(message for _, message in messages),
        portal_pages=dict(sorted(generator.portal_pages.items())),
        expected_vendors=_expected_vendors(config),
        answers=dict(sorted(generator.answers.items())),
    )


def _write_json(path: Path, content: object) -> None:
    text = json.dumps(content, indent=2, ensure_ascii=False) + "\n"
    path.write_bytes(text.encode("utf-8"))


def write_folder(seed: SeedData, folder: Path) -> None:
    """Writes the sample folder, replacing the emails and portal pages already there.

    Raises ValueError for a source account whose folder would not be directly inside
    the sample folder, since emails already in that folder are deleted.
    """
    accounts = sorted({m.source_account for m in seed.messages})
    for account in accounts:
        if (folder / account).resolve().parent != folder.resolve() or account == "portal":
            raise ValueError(f"not a usable name for a source account: {account!r}")
    for name, pattern in [*((a, "*.eml") for a in accounts), ("portal", "*.html")]:
        (folder / name).mkdir(parents=True, exist_ok=True)
        for stale in (folder / name).glob(pattern):
            stale.unlink()

    for message in seed.messages:
        (folder / message.source_account / message.file_name).write_bytes(message.raw)
    for name, html in seed.portal_pages.items():
        (folder / "portal" / name).write_bytes(html.encode("utf-8"))
    _write_json(folder / "golden.json", seed.golden)
    _write_json(folder / "expected_vendors.json", list(seed.expected_vendors))
    _write_json(folder / "answers.json", dict(seed.answers))


def load_messages(folder: Path, source_account: str) -> list[SeedMessage]:
    """The messages of one source account, read back from a written sample folder."""
    golden: list[GoldenEntry] = json.loads((folder / "golden.json").read_text(encoding="utf-8"))
    return [
        SeedMessage(
            source_account=source_account,
            file_name=entry["file_name"],
            message_id=entry["message_id"],
            raw=(folder / source_account / entry["file_name"]).read_bytes(),
            golden=entry,
        )
        for entry in golden
        if entry["source_account"] == source_account
    ]
