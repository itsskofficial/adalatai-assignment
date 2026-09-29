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

from invoice_collector import trail
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
from invoice_collector.archive import Archive, BothArchives, LocalArchive, is_named, pdf_sha256
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
    # What could not be tidied up after the decision was recorded. The decision stands.
    warnings: list[str] = []


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


class _NoLocalCopy(Exception):
    """The PDF of a held document is not on this machine, or cannot be told apart there."""


def _local_file(root: Path, month: CollectionMonth, document: PendingDocument) -> Path:
    """The PDF of a held document on this machine, kept under the ledger's folder.

    A document archived to Drive as well is linked to Drive. Its local copy is in the
    month's pending folder, or in the month folder when it waits only for another document
    of its email, under the name the run gave it or that name numbered. Documents with the
    same fields share that name, so the copy is the one whose SHA-256 the run recorded. It
    is not the content hash, which for a body or a portal page is the hash of its source.

    A ledger written before the SHA-256 was recorded names none: a single file of the name
    is taken, and with several nothing is guessed.

    Raises _NoLocalCopy, saying why.
    """
    missing = _NoLocalCopy(
        f"The PDF of {document.extraction.vendor} is not in the archive on this machine, "
        "so it cannot be filed."
    )
    if not _is_web_link(document.file_link):
        path = (root / document.file_link).resolve()
        if not (path.is_relative_to(root) and path.is_file()):
            raise missing
        # A file put in its place since the run saved it is not the document that was held.
        recorded = document.pdf_sha256
        if recorded is not None and pdf_sha256(path.read_bytes()) != recorded:
            raise _NoLocalCopy(
                f"The PDF of {document.extraction.vendor} on this machine is not the one the "
                "run saved: it was changed or replaced since. Nothing was changed."
            )
        return path
    name = filename(document.extraction)
    folder = root / ARCHIVE_FOLDER / str(month)
    candidates = [
        path
        for directory in (folder / "pending", folder)
        if directory.is_dir()
        for path in sorted(directory.iterdir())
        if path.is_file() and is_named(path.name, name)
    ]
    if document.pdf_sha256 is not None:
        for path in candidates:
            if pdf_sha256(path.read_bytes()) == document.pdf_sha256:
                return path
        raise missing
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise missing
    names = ", ".join(path.relative_to(folder).as_posix() for path in candidates)
    raise _NoLocalCopy(
        f"The PDF of {document.extraction.vendor} cannot be told apart on this machine: "
        f"{names} could each be it, and the ledger, written before the tool recorded which "
        "file a held document is, does not say. Nothing was changed; file it by hand."
    )


def _local_pdfs(
    root: Path, month: CollectionMonth, documents: Sequence[PendingDocument]
) -> dict[str, bytes]:
    """The PDF of each held document that is found on this machine, by content hash."""
    pdfs: dict[str, bytes] = {}
    for document in documents:
        try:
            pdfs[document.content_hash] = _local_file(root, month, document).read_bytes()
        except _NoLocalCopy:
            continue  # Reported when the pending copies are removed.
    return pdfs


def _remove_pending_copies(
    ledger: Ledger,
    archive: Archive,
    month: CollectionMonth,
    documents: Sequence[PendingDocument],
    pdfs: dict[str, bytes],
    decision: str,
) -> list[str]:
    """Removes the pending copy of each document no email holds any more, from every
    archive it was filed to. What cannot be removed is returned, as warnings.

    Called once the decision is recorded, so a copy that cannot be removed never undoes it.
    Whatever a removal raises is a warning, since the decision already stands.
    """
    still_held = {d.file_link for each in ledger.months() for d in ledger.pending(each)}
    warnings: list[str] = []
    for document in documents:
        if document.file_link in still_held:
            continue
        name = filename(document.extraction)
        pdf = pdfs.get(document.content_hash)
        if pdf is None:
            warnings.append(
                f"The copy of {name} in the pending folder was not found on this machine, "
                "or could not be told apart from another document of the same name, so it "
                "was left where it is; remove it by hand."
            )
            continue
        try:
            archive.remove(f"{month}/pending", name, pdf)
        except Exception as failure:
            warnings.append(
                f"The copy of {name} in the pending folder could not be removed ({failure}). "
                f"The {decision} stands; remove the copy by hand."
            )
    return warnings


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
    ) -> ReviewDecisionRecord:
        """Records the decision in the review history and any corrections in the golden
        dataset. Called as soon as the ledger has it, before anything is tidied up."""
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
        return record

    def remove_pending_copies(
        ledger: Ledger,
        month: CollectionMonth,
        item: _Item,
        pdfs: dict[str, bytes],
        decision: str,
    ) -> list[str]:
        """Tidies up after a recorded decision. Nothing raised here fails the request."""
        try:
            warnings = _remove_pending_copies(
                ledger, archive, month, item.documents, pdfs, decision
            )
        except Exception as failure:
            warnings = [
                f"The copies in the pending folder could not be removed ({failure}). "
                f"The {decision} stands; remove them by hand."
            ]
        if drive_archive is None and any(_is_web_link(d.file_link) for d in item.documents):
            warnings.append(
                "The copy in the pending folder in Google Drive was left where it is: the "
                "dashboard was started without the owner account. Remove it by hand."
            )
        return warnings

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
            try:
                path = _local_file(root, collection_month, document)
            except _NoLocalCopy:
                continue
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
            pdfs: dict[str, bytes] = {}
            for document in item.documents:
                try:
                    path = _local_file(root, collection_month, document)
                except _NoLocalCopy as missing:
                    raise HTTPException(status_code=409, detail=str(missing)) from None
                pdfs[document.content_hash] = path.read_bytes()

            collected: list[CollectedDocument] = []
            for document in item.documents:
                extraction = confirmed[document.content_hash]
                pdf = pdfs[document.content_hash]
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
                # What the document said is kept, whatever name the person confirmed.
                as_read = document.vendor_as_read or (
                    document.extraction.vendor
                    if extraction.vendor != document.extraction.vendor
                    else None
                )
                collected.append(
                    CollectedDocument(
                        document.content_hash, extraction, link, rate(extraction), as_read
                    )
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
            record = decided(collection_month, item, "approved", person, confirmed)
            warnings = remove_pending_copies(ledger, collection_month, item, pdfs, "approval")
        with opened() as ledger:
            ledger.record_events(_filed_on_approval(item, collected, person, record.decided_at))
        return _decision(record, warnings)

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
            pdfs = _local_pdfs(root, collection_month, item.documents)
            for held in item.emails:
                ledger.record(
                    collection_month,
                    held.email,
                    EmailState.SKIPPED,
                    reason=JUDGED_NOT_BILLING,
                    invoice_format=held.invoice_format,
                    portal_link=held.portal_link,
                )
            record = decided(collection_month, item, "rejected", person, None)
            # Nothing is archived: the PDF goes unless another email still holds it.
            warnings = remove_pending_copies(ledger, collection_month, item, pdfs, "rejection")
        return _decision(record, warnings)

    return router


def _decision(record: ReviewDecisionRecord, warnings: list[str] | None = None) -> ReviewDecision:
    return ReviewDecision(
        warnings=warnings or [],
        collection_month=record.collection_month,
        source_account=record.source_account,
        message_id=record.message_id,
        subject=record.subject,
        action=record.action,
        person=record.person,
        decided_at=record.decided_at.isoformat(),
        documents=[DocumentChange(**document) for document in record.documents],
    )


def _filed_on_approval(
    item: _Item, collected: Sequence[CollectedDocument], person: str, at: datetime
) -> list[trail.Event]:
    """The history of each approved document: where it was filed, and its rate to rupees."""
    email = item.first.email
    events: list[trail.Event] = []
    for document in collected:
        for kind, details in (
            (trail.FILED, trail.filed(document.file_link)),
            (trail.CONVERTED, trail.converted(document.extraction, document.inr_rate)),
        ):
            events.append(
                trail.Event(
                    kind=kind,
                    source_account=email.source_account,
                    message_id=email.message_id,
                    happened_at=at,
                    content_hash=document.content_hash,
                    actor=person,
                    details=details,
                )
            )
    return events
