"""What the Review screen shows and does: billing documents held for a person to confirm.

A run holds an email when it doubts what it read from a billing document in it. The PDF
waits in the month's pending folder and the document stays out of the summary. A person
either approves the email, confirming or correcting the fields of each held document, or
judges that it holds no billing document. Either decision is recorded in the ledger the
way a run records an email, so a later run treats it as settled. See ADR 0005 and 0006.

Corrections are appended to a file beside the ledger, from which they are added to the
golden dataset. See ADR 0004.
"""

import json
import re
import sqlite3
from collections.abc import Callable, Generator, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import median
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi import Path as PathParameter
from fastapi.responses import FileResponse
from google.auth.exceptions import GoogleAuthError
from googleapiclient.errors import HttpError
from pydantic import BaseModel

from invoice_collector.api.month_summary import file_name
from invoice_collector.api.review_history import (
    DocumentDecision,
    ReviewAction,
    ReviewDecisionRecord,
    ReviewHistory,
    amount,
    changed_fields,
    fields_of,
)
from invoice_collector.archive import Archive, BothArchives, LocalArchive
from invoice_collector.domain import (
    CollectionMonth,
    DocumentType,
    Email,
    EmailState,
    Extraction,
    Field,
    InvoiceFormat,
)
from invoice_collector.exchange_rates import ExchangeRates, ExchangeRateUnavailable
from invoice_collector.ledger import CollectedDocument, Ledger, PendingDocument
from invoice_collector.naming import filename
from invoice_collector.reconciler import vendor_key
from invoice_collector.run import JUDGED_NOT_BILLING

MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
CORRECTIONS_FILE = "corrections.jsonl"
ARCHIVE_FOLDER = "archive"
CURRENCY_PATTERN = re.compile(r"^[A-Za-z]{3}$")
DATE_PATTERN = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
DOCUMENT_TYPES: tuple[DocumentType, ...] = ("invoice", "receipt", "credit_note")

Month = Annotated[
    str, PathParameter(pattern=MONTH_PATTERN, description="Collection month, YYYY-MM")
]
LedgerFactory = Callable[[], Ledger]
PersonDependency = Callable[..., str]


class DoubtView(BaseModel):
    field: Field | None
    reason: str


class HeldDocument(BaseModel):
    content_hash: str
    document_type: DocumentType
    vendor: str
    invoice_date: str
    total: str
    currency: str
    doubts: list[DoubtView]
    read_again: bool
    file_name: str
    file_url: str
    usual_amount: str | None
    usual_currency: str | None
    source_accounts: list[str]


class ReviewItem(BaseModel):
    source_account: str
    message_id: str
    sender: str
    subject: str
    received_at: str
    invoice_format: InvoiceFormat | None
    portal_link: str | None
    reason: str | None
    # An email with no billing document to confirm: the portal link needs a person to sign
    # in and download it. It cannot be approved here.
    needs_manual_download: bool
    documents: list[HeldDocument]
    # Every email the item stands for: the same documents found in other source accounts.
    message_ids: list[str]


class ReviewQueue(BaseModel):
    month: str
    items: list[ReviewItem]


class ConfirmedFields(BaseModel):
    """The fields of one held billing document as a person confirms them.

    The types are loose so that a wrong value is refused with a plain message.
    """

    content_hash: str
    vendor: str | None = None
    invoice_date: str | None = None
    total: str | int | float | None = None
    currency: str | None = None
    document_type: str | None = None


class Approval(BaseModel):
    documents: list[ConfirmedFields]


class DocumentChange(BaseModel):
    content_hash: str
    before: dict[str, str]
    after: dict[str, str] | None
    changed_fields: list[str]


class ReviewDecision(BaseModel):
    collection_month: str
    source_account: str
    message_id: str
    subject: str
    action: ReviewAction
    person: str
    decided_at: str
    documents: list[DocumentChange]


@dataclass(frozen=True)
class FieldProblem:
    content_hash: str | None
    field: str | None
    reason: str


@dataclass(frozen=True)
class _HeldEmail:
    email: Email
    reason: str | None
    invoice_format: InvoiceFormat | None
    portal_link: str | None


@dataclass(frozen=True)
class _Item:
    """One entry in the queue: an email, and any other emails holding the same documents."""

    emails: list[_HeldEmail]
    documents: list[PendingDocument]

    @property
    def first(self) -> _HeldEmail:
        return self.emails[0]


def _is_web_link(link: str) -> bool:
    return link.startswith(("https://", "http://"))


def _held_emails(ledger_path: Path, month: CollectionMonth) -> list[_HeldEmail]:
    """The month's emails that need review, with their sender and when they arrived.

    The Ledger gives neither for an email it holds no collected document of, so this reads
    its SQLite file directly, read-only, as months.py does.
    """
    if not ledger_path.is_file():
        return []
    uri = f"{ledger_path.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        rows = db.execute(
            "SELECT source_account, message_id, sender, subject, received_at, reason, "
            "invoice_format, portal_link FROM emails WHERE collection_month = ? AND state = ? "
            "ORDER BY received_at, source_account, message_id",
            (str(month), EmailState.NEEDS_REVIEW.value),
        ).fetchall()
    return [
        _HeldEmail(
            email=Email(
                source_account=account,
                message_id=message_id,
                sender=sender,
                subject=subject,
                received_at=datetime.fromisoformat(received_at),
            ),
            reason=reason,
            invoice_format=InvoiceFormat(invoice_format) if invoice_format else None,
            portal_link=portal_link,
        )
        for (
            account,
            message_id,
            sender,
            subject,
            received_at,
            reason,
            invoice_format,
            portal_link,
        ) in rows
    ]


def _items(ledger: Ledger, ledger_path: Path, month: CollectionMonth) -> list[_Item]:
    """The queue. Emails holding the same documents, found in several source accounts,
    are one item, so a person confirms each document once."""
    held_by: dict[tuple[str, str], list[PendingDocument]] = {}
    for document in ledger.pending(month):
        held_by.setdefault((document.source_account, document.message_id), []).append(document)

    items: list[_Item] = []
    by_documents: dict[frozenset[str], _Item] = {}
    for held in _held_emails(ledger_path, month):
        key = (held.email.source_account, held.email.message_id)
        documents = held_by.get(key, [])
        hashes = frozenset(d.content_hash for d in documents)
        if hashes and hashes in by_documents:
            by_documents[hashes].emails.append(held)
            continue
        item = _Item([held], documents)
        items.append(item)
        if hashes:
            by_documents[hashes] = item
    return items


def _item_of(
    ledger: Ledger, ledger_path: Path, month: CollectionMonth, account: str, message: str
) -> _Item:
    for item in _items(ledger, ledger_path, month):
        if any(
            (h.email.source_account, h.email.message_id) == (account, message) for h in item.emails
        ):
            return item
    raise HTTPException(
        status_code=404, detail=f"No email {message} in {account} needs review in {month}"
    )


def usual_for(
    ledger: Ledger, month: CollectionMonth, vendor: str
) -> tuple[Decimal | None, str | None]:
    """What the vendor usually bills: from the expected vendor list, else earlier months.

    The same rule the run applies when it compares a total with the usual one.
    """
    key = vendor_key(vendor)
    listed = [
        v
        for v in ledger.expected_vendors()
        if v.status == "expected" and vendor_key(v.vendor) == key and v.usual_amount
    ]
    if listed:
        return listed[0].usual_amount, listed[0].currency
    earlier = [
        d.extraction
        for each in ledger.months()
        if (each.year, each.month) < (month.year, month.month)
        for d in ledger.documents(each)
        if vendor_key(d.extraction.vendor) == key and d.extraction.document_type != "credit_note"
    ]
    if not earlier:
        return None, None
    latest = max(earlier, key=lambda e: e.invoice_date)
    same = [e.total for e in earlier if e.currency == latest.currency]
    return Decimal(median(same)).quantize(Decimal("0.01")), latest.currency


def _file_url(month: CollectionMonth, link: str) -> str:
    if _is_web_link(link):
        return link
    return f"/api/months/{month}/review/billing-documents/{quote(file_name(link), safe='')}"


def _view(
    ledger: Ledger, month: CollectionMonth, item: _Item, accounts: dict[str, list[str]]
) -> ReviewItem:
    first = item.first
    documents: list[HeldDocument] = []
    for document in item.documents:
        extraction = document.extraction
        usual, usual_currency = usual_for(ledger, month, extraction.vendor)
        documents.append(
            HeldDocument(
                content_hash=document.content_hash,
                document_type=extraction.document_type,
                vendor=extraction.vendor,
                invoice_date=extraction.invoice_date.isoformat(),
                total=amount(extraction.total),
                currency=extraction.currency,
                doubts=[DoubtView(field=d.field, reason=d.reason) for d in document.doubts],
                read_again=document.read_again,
                file_name=file_name(document.file_link),
                file_url=_file_url(month, document.file_link),
                usual_amount=f"{usual:.2f}" if usual is not None else None,
                usual_currency=usual_currency,
                source_accounts=accounts.get(document.content_hash, []),
            )
        )
    return ReviewItem(
        source_account=first.email.source_account,
        message_id=first.email.message_id,
        sender=first.email.sender,
        subject=first.email.subject,
        received_at=first.email.received_at.isoformat(),
        invoice_format=first.invoice_format,
        portal_link=first.portal_link,
        reason=first.reason,
        needs_manual_download=not item.documents,
        documents=documents,
        message_ids=[held.email.message_id for held in item.emails],
    )


def review_queue(ledger: Ledger, ledger_path: Path, month: CollectionMonth) -> ReviewQueue:
    items = _items(ledger, ledger_path, month)
    accounts: dict[str, set[str]] = {}
    for document in ledger.pending(month):
        accounts.setdefault(document.content_hash, set()).add(document.source_account)
    found_in = {digest: sorted(each) for digest, each in accounts.items()}
    return ReviewQueue(
        month=str(month), items=[_view(ledger, month, item, found_in) for item in items]
    )


# Checking what a person confirmed


def _confirmed(
    month: CollectionMonth, given: ConfirmedFields
) -> tuple[Extraction | None, list[FieldProblem]]:
    """The fields as confirmed, or the problems with them."""
    problems: list[FieldProblem] = []

    def refuse(field: str, reason: str) -> None:
        problems.append(FieldProblem(given.content_hash, field, reason))

    vendor = (given.vendor or "").strip()
    if not vendor:
        refuse("vendor", "The vendor must not be blank")

    day: date | None = None
    text = (given.invoice_date or "").strip()
    try:
        if not DATE_PATTERN.match(text):
            raise ValueError(text)
        day = date.fromisoformat(text)
    except ValueError:
        refuse("invoice_date", "The invoice date must be a real date, as YYYY-MM-DD")
    if day is not None and not month.contains(day):
        refuse(
            "invoice_date",
            f"The invoice date {day.isoformat()} belongs to collection month "
            f"{CollectionMonth.of(day)}, not {month}",
        )

    total: Decimal | None = None
    try:
        total = Decimal(str(given.total).strip()) if given.total is not None else None
    except InvalidOperation:
        total = None
    if total is None or not total.is_finite():
        refuse("total", "The total must be a number, such as 652.50")
        total = None

    currency = (given.currency or "").strip()
    if not CURRENCY_PATTERN.match(currency):
        refuse("currency", "The currency must be three letters, such as USD")

    document_type: DocumentType | None = None
    for kind in DOCUMENT_TYPES:
        if kind == given.document_type:
            document_type = kind
    if document_type is None:
        refuse("document_type", "The document type must be invoice, receipt or credit_note")

    if problems or day is None or total is None or document_type is None:
        return None, problems
    if document_type == "credit_note":
        # A credit note records money returned, whatever sign was typed.
        total = -abs(total)
    return Extraction(document_type, vendor, day, total, currency.upper()), []


def _all_confirmed(
    month: CollectionMonth, held: Sequence[PendingDocument], approval: Approval
) -> dict[str, Extraction]:
    """Each held document's confirmed fields, by content hash, or a 422 naming each problem."""
    problems: list[FieldProblem] = []
    given: dict[str, ConfirmedFields] = {}
    for fields in approval.documents:
        if fields.content_hash in given:
            problems.append(
                FieldProblem(fields.content_hash, None, "This document is confirmed twice")
            )
        given[fields.content_hash] = fields
    known = {document.content_hash for document in held}
    for digest in given.keys() - known:
        problems.append(FieldProblem(digest, None, "This document is not held in this email"))

    confirmed: dict[str, Extraction] = {}
    for document in held:
        fields = given.get(document.content_hash)
        if fields is None:
            problems.append(
                FieldProblem(document.content_hash, None, "Every held document must be confirmed")
            )
            continue
        extraction, found = _confirmed(month, fields)
        problems.extend(found)
        if extraction is not None:
            confirmed[document.content_hash] = extraction

    if problems:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Nothing was changed. Correct the fields named and approve again.",
                "problems": [
                    {"content_hash": p.content_hash, "field": p.field, "reason": p.reason}
                    for p in problems
                ],
            },
        )
    return confirmed


# Files


def _local_file(root: Path, month: CollectionMonth, document: PendingDocument) -> Path | None:
    """The PDF of a held document on this machine, if it is kept under the ledger's folder.

    A document archived to Drive as well is linked to Drive; its local copy has the name
    the run gave it.
    """
    if _is_web_link(document.file_link):
        name = filename(document.extraction)
        folder = root / ARCHIVE_FOLDER / str(month)
        candidates = [folder / "pending" / name, folder / name]
    else:
        candidates = [root / document.file_link]
    for candidate in candidates:
        path = candidate.resolve()
        if path.is_relative_to(root) and path.is_file():
            return path
    return None


def _links_in_use(ledger: Ledger, root: Path) -> set[Path]:
    """Every local file the ledger still names, held or collected, in any month."""
    links = [
        link
        for month in ledger.months()
        for link in [d.file_link for d in ledger.documents(month)]
        + [d.file_link for d in ledger.pending(month)]
        if not _is_web_link(link)
    ]
    return {(root / link).resolve() for link in links}


def _remove_unused(ledger: Ledger, root: Path, paths: Sequence[Path]) -> None:
    in_use = _links_in_use(ledger, root)
    for path in paths:
        if path not in in_use and path.is_relative_to(root):
            path.unlink(missing_ok=True)
            folder = path.parent
            # A pending folder with nothing left to review goes too.
            if folder.name == "pending" and not any(folder.iterdir()):
                folder.rmdir()


def _append_corrections(
    path: Path,
    month: CollectionMonth,
    item: _Item,
    confirmed: dict[str, Extraction],
    person: str,
    at: datetime,
) -> None:
    """Adds a record for each document whose confirmed fields differ from what was read."""
    lines: list[str] = []
    for document in item.documents:
        after = confirmed[document.content_hash]
        changed = changed_fields(document.extraction, after)
        if not changed:
            continue
        record: dict[str, Any] = {
            "content_hash": document.content_hash,
            "collection_month": str(month),
            "source_account": item.first.email.source_account,
            "message_id": item.first.email.message_id,
            "extracted": fields_of(document.extraction),
            "confirmed": fields_of(after),
            "changed_fields": changed,
            "doubts": [{"field": d.field, "reason": d.reason} for d in document.doubts],
            "read_again": document.read_again,
            "person": person,
            "corrected_at": at.isoformat(),
        }
        lines.append(json.dumps(record, ensure_ascii=False))
    if lines:
        with path.open("a", encoding="utf-8") as corrections:
            corrections.write("".join(f"{line}\n" for line in lines))


def review_routes(
    ledger_factory: LedgerFactory,
    ledger_path: Path,
    exchange_rates: ExchangeRates,
    signed_in_person: PersonDependency,
    now: Callable[[], datetime],
    drive_archive: Archive | None = None,
) -> APIRouter:
    """The Review screen's routes.

    An approved document is filed to the local archive beside the ledger and, when the
    dashboard is given the owner account's Drive, to Drive first, as a run with an owner
    account files it: the ledger then links to the copy in Drive.
    """
    router = APIRouter(prefix="/months/{month}/review")
    history = ReviewHistory(ledger_path)
    root = ledger_path.parent.resolve()
    local_archive = LocalArchive(root / ARCHIVE_FOLDER)
    archive: Archive = (
        BothArchives(drive_archive, local_archive) if drive_archive is not None else local_archive
    )

    @contextmanager
    def opened() -> Generator[Ledger]:
        # Opened inside each route, since SQLite keeps a connection to its own thread.
        ledger = ledger_factory()
        try:
            yield ledger
        finally:
            ledger.close()

    def rate(extraction: Extraction) -> Decimal | None:
        try:
            return exchange_rates.to_rupees(extraction.currency, extraction.invoice_date)
        except ExchangeRateUnavailable:
            return None  # The rupee amount is left empty, as a run leaves it.

    def decided(
        month: CollectionMonth,
        item: _Item,
        action: ReviewAction,
        person: str,
        confirmed: dict[str, Extraction] | None,
    ) -> ReviewDecision:
        at = now()
        documents = [
            DocumentDecision(
                d.content_hash, d.extraction, confirmed[d.content_hash] if confirmed else None
            )
            for d in item.documents
        ]
        email = item.first.email
        record = history.record(
            month,
            source_account=email.source_account,
            message_id=email.message_id,
            subject=email.subject,
            action=action,
            person=person,
            decided_at=at,
            documents=documents,
        )
        if confirmed is not None:
            _append_corrections(
                ledger_path.parent / CORRECTIONS_FILE, month, item, confirmed, person, at
            )
        return _decision(record)

    def held_item(ledger: Ledger, month: CollectionMonth, account: str, message: str) -> _Item:
        item = _item_of(ledger, ledger_path, month, account, message)
        if not item.documents:
            raise HTTPException(
                status_code=409,
                detail="This email holds no billing document to confirm. Download it from "
                "the portal link and upload it instead.",
            )
        return item

    @router.get("")
    def queue(month: Month) -> ReviewQueue:  # pyright: ignore[reportUnusedFunction]
        with opened() as ledger:
            return review_queue(ledger, ledger_path, CollectionMonth.parse(month))

    @router.get("/history")
    def decisions(month: Month) -> list[ReviewDecision]:  # pyright: ignore[reportUnusedFunction]
        return [_decision(record) for record in history.of(CollectionMonth.parse(month))]

    @router.get("/billing-documents/{name}")
    def held_document(month: Month, name: str) -> FileResponse:  # pyright: ignore[reportUnusedFunction]
        collection_month = CollectionMonth.parse(month)
        with opened() as ledger:
            held = ledger.pending(collection_month)
        for document in held:
            if _is_web_link(document.file_link) or file_name(document.file_link) != name:
                continue
            path = _local_file(root, collection_month, document)
            if path is not None:
                return FileResponse(
                    path, media_type="application/pdf", content_disposition_type="inline"
                )
        raise HTTPException(status_code=404, detail="No such billing document")

    @router.post("/{source_account}/{message_id}/approve")
    def approve(  # pyright: ignore[reportUnusedFunction]
        month: Month,
        source_account: str,
        message_id: str,
        approval: Annotated[Approval, Body()],
        person: Annotated[str, Depends(signed_in_person)],
    ) -> ReviewDecision:
        collection_month = CollectionMonth.parse(month)
        with opened() as ledger:
            item = held_item(ledger, collection_month, source_account, message_id)
            confirmed = _all_confirmed(collection_month, item.documents, approval)
            files: dict[str, Path] = {}
            for document in item.documents:
                path = _local_file(root, collection_month, document)
                if path is None:
                    raise HTTPException(
                        status_code=409,
                        detail=f"The PDF of {document.extraction.vendor} is not in the archive "
                        "on this machine, so it cannot be filed.",
                    )
                files[document.content_hash] = path

            collected: list[CollectedDocument] = []
            for document in item.documents:
                extraction = confirmed[document.content_hash]
                pdf = files[document.content_hash].read_bytes()
                try:
                    # The archive never overwrites a different document with the same name.
                    link = archive.save(str(collection_month), filename(extraction), pdf)
                except (OSError, HttpError, GoogleAuthError):
                    # Nothing is recorded, so the email stays held and approving it again
                    # files it. A copy already saved is found again, not saved twice.
                    raise HTTPException(
                        status_code=502,
                        detail="The billing document could not be filed to the archive. "
                        "Nothing was changed; try approving again.",
                    ) from None
                collected.append(
                    CollectedDocument(document.content_hash, extraction, link, rate(extraction))
                )
            for held in item.emails:
                ledger.record(
                    collection_month,
                    held.email,
                    EmailState.COLLECTED,
                    invoice_format=held.invoice_format,
                    portal_link=held.portal_link,
                    documents=tuple(collected),
                )
            _remove_unused(ledger, root, list(files.values()))
        return decided(collection_month, item, "approved", person, confirmed)

    @router.post("/{source_account}/{message_id}/reject")
    def reject(  # pyright: ignore[reportUnusedFunction]
        month: Month,
        source_account: str,
        message_id: str,
        person: Annotated[str, Depends(signed_in_person)],
    ) -> ReviewDecision:
        collection_month = CollectionMonth.parse(month)
        with opened() as ledger:
            item = held_item(ledger, collection_month, source_account, message_id)
            files = [
                path
                for document in item.documents
                if (path := _local_file(root, collection_month, document)) is not None
            ]
            for held in item.emails:
                ledger.record(
                    collection_month,
                    held.email,
                    EmailState.SKIPPED,
                    reason=JUDGED_NOT_BILLING,
                    invoice_format=held.invoice_format,
                    portal_link=held.portal_link,
                )
            # Nothing is archived: the PDF goes unless another email still holds it.
            _remove_unused(ledger, root, files)
        return decided(collection_month, item, "rejected", person, None)

    return router


def _decision(record: ReviewDecisionRecord) -> ReviewDecision:
    return ReviewDecision(
        collection_month=record.collection_month,
        source_account=record.source_account,
        message_id=record.message_id,
        subject=record.subject,
        action=record.action,
        person=record.person,
        decided_at=record.decided_at.isoformat(),
        documents=[DocumentChange(**document) for document in record.documents],
    )
