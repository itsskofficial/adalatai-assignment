"""Assisted downloads: a PDF a person fetched from a login-gated portal, handed to the tool.

A run flags an email whose portal link leads to a sign-in page: it needs review, with no
billing document to confirm, and the Review screen lists it apart with its link. A person
opens the link, signs in, downloads the PDF and uploads it here. The PDF is then treated
as the run treats a document it fetched itself: read, checked, and either filed and
reported or held for review with the reasons. See ADR 0005, 0006, 0012 and 0013.

The uploaded file is untrusted, like everything an email leads to (ADR 0012). It is
accepted only if its bytes are a PDF, whatever it is called; its size is limited; it is
never rendered or run here; and what it says is only read as data by the extractor.

A billing document is known by what it was made from (ADR 0013). A document behind a
portal link is known by the link's address, so an uploaded one is recorded under that
identity: a later run recognises it and neither fetches the link again nor takes it away.
The same bytes are also recognised: uploading the same file again changes nothing, and a
file already collected as an attachment is linked to that document, not filed twice. The
bytes are recorded in the ledger as another identity of the document, so a run that finds
them attached to an email later links that email to it too.

Its vendor is matched to the expected vendor list as a run matches one (ADR 0016), and the
upload is a step in the document's history, followed by the steps a run records for a
document it reads (see trail.py).
"""

import io
import sqlite3
import threading
from collections.abc import Callable, Generator, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi import Path as PathParameter
from google.auth.exceptions import GoogleAuthError
from googleapiclient.errors import HttpError
from pydantic import BaseModel
from pypdf import PdfReader
from starlette.concurrency import run_in_threadpool

from invoice_collector import tracing, trail
from invoice_collector.api.assisted_download_history import (
    AssistedDownloadHistory,
    UploadOutcome,
    UploadRecord,
)
from invoice_collector.api.month_summary import file_name
from invoice_collector.api.review import ARCHIVE_FOLDER, MONTH_PATTERN, DoubtView, usual_for
from invoice_collector.api.review_history import fields_of
from invoice_collector.archive import Archive, BothArchives, LocalArchive
from invoice_collector.checks import History, history_checks, reading_checks, summary_of
from invoice_collector.domain import (
    CollectionMonth,
    DocumentType,
    Doubt,
    Email,
    EmailState,
    Extraction,
    InvoiceFormat,
)
from invoice_collector.exchange_rates import ExchangeRates, ExchangeRateUnavailable
from invoice_collector.extractor import (
    ExtractionFailed,
    Extractor,
    NotABillingDocument,
    content_hash,
    pdf_text,
)
from invoice_collector.ledger import CollectedDocument, DocumentRecord, Ledger, PendingDocument
from invoice_collector.naming import filename
from invoice_collector.reconciler import vendor_key
from invoice_collector.run import Settings as RunSettings
from invoice_collector.run import expected_vendor_for
from invoice_collector.vendor_matcher import (
    RulesFirstVendorMatcher,
    VendorMatcher,
    VendorMatchFailed,
)

# A billing document is a few hundred kilobytes. The limit leaves room for a scanned one,
# and keeps the PDF within what the model accepts once it is encoded for sending.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

Month = Annotated[
    str, PathParameter(pattern=MONTH_PATTERN, description="Collection month, YYYY-MM")
]
LedgerFactory = Callable[[], Ledger]
PersonDependency = Callable[..., str]


class UploadedDocument(BaseModel):
    document_type: DocumentType
    vendor: str
    invoice_date: str
    total: str
    currency: str
    doubts: list[DoubtView]


class AssistedDownload(BaseModel):
    """An upload, and what became of it."""

    outcome: UploadOutcome
    # The collection month the billing document was filed or held under: the month of
    # its invoice date.
    collection_month: str
    source_account: str
    message_id: str
    subject: str
    portal_link: str | None
    file_name: str
    size: int
    document: UploadedDocument
    person: str
    uploaded_at: str


@dataclass(frozen=True)
class _Flagged:
    """An email that needs review, as the ledger holds it."""

    email: Email
    invoice_format: InvoiceFormat | None
    portal_link: str | None


def _needing_review(ledger_path: Path, month: CollectionMonth) -> list[_Flagged]:
    """The month's emails that need review. Read directly from the ledger's file, read-only,
    as the Review screen reads them, since the Ledger gives no sender for them."""
    if not ledger_path.is_file():
        return []
    uri = f"{ledger_path.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        rows = db.execute(
            "SELECT source_account, message_id, sender, subject, received_at, invoice_format, "
            "portal_link FROM emails WHERE collection_month = ? AND state = ? "
            "ORDER BY received_at, source_account, message_id",
            (str(month), EmailState.NEEDS_REVIEW.value),
        ).fetchall()
    return [
        _Flagged(
            Email(
                source_account=account,
                message_id=message_id,
                sender=sender,
                subject=subject,
                received_at=datetime.fromisoformat(received_at),
            ),
            InvoiceFormat(invoice_format) if invoice_format else None,
            portal_link,
        )
        for account, message_id, sender, subject, received_at, invoice_format, portal_link in rows
    ]


def pdf_problem(pdf: bytes) -> str | None:
    """Why the bytes are not a PDF the tool will take, or None if they are.

    Judged by the content alone. The structure is read to count the pages; nothing in the
    file is rendered or run.
    """
    if not pdf:
        return "No file was uploaded. Choose the PDF downloaded from the portal."
    if not pdf.startswith(b"%PDF-"):
        return "The file is not a PDF. Upload the PDF downloaded from the portal."
    try:
        reader = PdfReader(io.BytesIO(pdf))
        if reader.is_encrypted:
            return "The PDF is protected with a password. Download a copy without one."
        if len(reader.pages) == 0:
            return "The PDF has no pages."
    except Exception:  # A damaged or hostile file may fail in any way while it is parsed.
        return "The file is not a readable PDF. Download it from the portal again."
    return None


async def _body(request: Request) -> bytes:
    """The uploaded bytes, refused as soon as they pass the limit."""
    too_large = HTTPException(
        status_code=413,
        detail=f"The file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB. "
        "A billing document is much smaller; check that this is the right file.",
    )
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_UPLOAD_BYTES:
        raise too_large
    received = bytearray()
    async for chunk in request.stream():
        received.extend(chunk)
        if len(received) > MAX_UPLOAD_BYTES:
            raise too_large
    return bytes(received)


def _as_charged(extraction: Extraction) -> Extraction:
    """A credit note records money returned, so its amount is negative, as a run records it."""
    if extraction.document_type == "credit_note":
        return replace(extraction, total=-abs(extraction.total))
    return extraction


def _history(
    ledger: Ledger, month: CollectionMonth, extraction: Extraction, identity: str
) -> History:
    """What is known of the vendor, by the rules a run applies."""
    usual, currency = usual_for(ledger, month, extraction.vendor)
    key = vendor_key(extraction.vendor)
    this_month = {
        d.content_hash
        for d in ledger.documents(month)
        if vendor_key(d.extraction.vendor) == key
        and d.extraction.document_type == extraction.document_type
        and d.content_hash != identity
    }
    return History(usual, currency, already_this_month=len(this_month))


def _document_view(extraction: Extraction, doubts: Sequence[Doubt]) -> dict[str, Any]:
    return {
        "fields": fields_of(extraction),
        "doubts": [{"field": d.field, "reason": d.reason} for d in doubts],
    }


def _view(record: UploadRecord) -> AssistedDownload:
    fields: dict[str, Any] = record.document["fields"]
    return AssistedDownload(
        outcome=record.outcome,
        collection_month=record.collection_month,
        source_account=record.source_account,
        message_id=record.message_id,
        subject=record.subject,
        portal_link=record.portal_link,
        file_name=record.file_name,
        size=record.size,
        document=UploadedDocument(
            document_type=fields["document_type"],
            vendor=fields["vendor"],
            invoice_date=fields["invoice_date"],
            total=fields["total"],
            currency=fields["currency"],
            doubts=[DoubtView(**doubt) for doubt in record.document["doubts"]],
        ),
        person=record.person,
        uploaded_at=record.uploaded_at.isoformat(),
    )


@dataclass(frozen=True)
class _Reading:
    extraction: Extraction
    doubts: tuple[Doubt, ...]
    read_again: bool


class _Steps:
    """The steps of an upload in the history of its billing document, as a run records the
    steps of a document it fetched itself. Recorded with the outcome. See trail.py."""

    def __init__(self, at: datetime) -> None:
        self.at = at
        self.events: list[trail.Event] = []

    def add(
        self,
        email: Email,
        kind: str,
        actor: str | None,
        details: dict[str, Any],
        identity: str | None,
    ) -> None:
        self.events.append(
            trail.Event(
                kind=kind,
                source_account=email.source_account,
                message_id=email.message_id,
                happened_at=self.at,
                content_hash=identity,
                actor=actor,
                details=details,
            )
        )

    def checked(self, email: Email, stage: str, results: Sequence[Any], identity: str) -> None:
        self.add(email, trail.CHECKED, trail.RUN, trail.checked(stage, results), identity)


def assisted_download_routes(
    ledger_factory: LedgerFactory,
    ledger_path: Path,
    extractor: Extractor | None,
    exchange_rates: ExchangeRates,
    signed_in_person: PersonDependency,
    now: Callable[[], datetime],
    drive_archive: Archive | None = None,
    stronger_extractor: Extractor | None = None,
    vendor_matcher: VendorMatcher | None = None,
) -> APIRouter:
    """The routes for uploading a PDF downloaded by hand from a login-gated portal.

    They sit beside the Review screen's, which lists the emails waiting for one. A filed
    upload goes where an approved document goes: to the owner account's Drive first when
    the dashboard has it, and to the local archive beside the ledger. Its vendor is matched
    to the expected vendor list as a run matches one (ADR 0016); without a matcher, by
    rules alone.
    """
    router = APIRouter(prefix="/months/{month}/review")
    history = AssistedDownloadHistory(ledger_path)
    local_archive = LocalArchive(ledger_path.parent.resolve() / ARCHIVE_FOLDER)
    archive: Archive = (
        BothArchives(drive_archive, local_archive) if drive_archive is not None else local_archive
    )
    threshold = RunSettings().anomaly_threshold
    matcher: VendorMatcher = vendor_matcher or RulesFirstVendorMatcher()
    # One upload is decided at a time, so a file sent twice at once is filed once.
    deciding = threading.Lock()

    @contextmanager
    def opened() -> Generator[Ledger]:
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

    def read(pdf: bytes, email: Email, identity: str, steps: _Steps) -> _Reading:
        if extractor is None:
            raise HTTPException(
                status_code=503,
                detail="Uploads cannot be read just now: the dashboard was started without a "
                "way to read billing documents. Nothing was changed.",
            )
        try:
            with tracing.scope(step=tracing.EXTRACTION, document=identity) as traced:
                extraction = _as_charged(extractor.extract(pdf))
        except NotABillingDocument as finding:
            raise HTTPException(
                status_code=422,
                detail=f"This PDF does not look like a billing document ({finding}). Nothing "
                "was changed; check that it is the file from the portal link.",
            ) from None
        except ExtractionFailed as failure:
            raise HTTPException(
                status_code=502,
                detail=f"The PDF could not be read ({failure}). Nothing was changed; try "
                "uploading it again.",
            ) from None
        details = {**trail.read(extraction), **traced.details()}
        steps.add(email, trail.READ, extraction.by, details, identity)
        results = reading_checks(extraction, email)
        steps.checked(email, "reading", results, identity)
        doubts = [doubt for result in results for doubt in result.doubts]
        read_again = False
        # Only a doubt about the reading is worth a second reading, as in a run.
        if doubts and stronger_extractor is not None:
            traced = tracing.scope(step=tracing.ESCALATED_EXTRACTION, document=identity)
            try:
                with traced:
                    second = _as_charged(stronger_extractor.extract(pdf))
            except (ExtractionFailed, NotABillingDocument) as failure:
                # The first reading stands, with its doubts.
                details = {"reason": str(failure), **traced.details()}
                steps.add(email, trail.READ_AGAIN_FAILED, None, details, identity)
            else:
                read_again = True
                details = {**trail.read_again(extraction, second), **traced.details()}
                steps.add(email, trail.READ_AGAIN, second.by, details, identity)
                extraction = second
                results = reading_checks(extraction, email)
                steps.checked(email, "reading", results, identity)
                doubts = [doubt for result in results for doubt in result.doubts]
        return _Reading(extraction, tuple(doubts), read_again)

    def as_expected(
        ledger: Ledger,
        extraction: Extraction,
        pdf: bytes,
        email: Email,
        identity: str,
        steps: _Steps,
    ) -> tuple[Extraction, str | None]:
        """The extraction under the expected vendor's spelling, and the name it replaced.

        Matched from the document's own text, as a run matches one, or from the sender and
        subject of the email, which is all the ledger keeps of it. A matcher that fails
        leaves the name as read, as it does in a run.
        """
        text = pdf_text(pdf)[0] or f"{email.sender}\n{email.subject}"
        try:
            with tracing.scope(step=tracing.VENDOR_MATCHING, document=identity) as traced:
                match = expected_vendor_for(extraction.vendor, text, ledger, matcher)
        except VendorMatchFailed:
            return extraction, None
        if match.vendor is None or match.vendor == extraction.vendor:
            return extraction, None
        details = {**trail.matched(extraction.vendor, match.vendor), **traced.details()}
        steps.add(email, trail.MATCHED, match.by, details, identity)
        return replace(extraction, vendor=match.vendor), extraction.vendor

    def save(folder: str, extraction: Extraction, pdf: bytes) -> str:
        try:
            # The archive never overwrites a different document with the same name.
            return archive.save(folder, filename(extraction), pdf)
        except (OSError, HttpError, GoogleAuthError):
            raise HTTPException(
                status_code=502,
                detail="The billing document could not be filed to the archive. Nothing was "
                "changed; try uploading it again.",
            ) from None

    def known_document(ledger: Ledger, file_hash: str, identity: str) -> DocumentRecord | None:
        """The billing document already collected from these bytes or from this portal link."""
        found = ledger.document_with(file_hash)
        if found is not None:
            return found
        for earlier in history.of_file(file_hash):
            found = ledger.document_with(earlier.content_hash)
            if found is not None:
                return found
        return ledger.document_with(identity)

    def held_document(ledger: Ledger, file_hash: str, identity: str) -> PendingDocument | None:
        """The billing document from these bytes or this portal link already held."""
        for digest in (file_hash, *(e.content_hash for e in history.of_file(file_hash))):
            found = ledger.pending_with(digest)
            if found is not None:
                return found
        return ledger.pending_with(identity)

    def upload(
        month: CollectionMonth, account: str, message: str, pdf: bytes, person: str
    ) -> AssistedDownload:
        file_hash = content_hash(pdf)
        steps = _Steps(now())
        with deciding, opened() as ledger:
            flagged = _needing_review(ledger_path, month)
            holding = {(d.source_account, d.message_id) for d in ledger.pending(month)}
            waiting = [
                f for f in flagged if (f.email.source_account, f.email.message_id) not in holding
            ]
            target = next(
                (
                    w
                    for w in waiting
                    if (w.email.source_account, w.email.message_id) == (account, message)
                ),
                None,
            )
            if target is None:
                # The same file sent again, after a slow answer or a second click, is
                # answered as the first time and changes nothing.
                for earlier in history.of_email(account, message):
                    if earlier.file_hash == file_hash:
                        return _view(earlier)
                if any(
                    (f.email.source_account, f.email.message_id) == (account, message)
                    for f in flagged
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="This email holds a billing document to review. Approve or "
                        "reject it on the Review screen instead.",
                    )
                raise HTTPException(
                    status_code=404,
                    detail=f"No email {message} in {account} is waiting for a download in {month}",
                )

            problem = pdf_problem(pdf)
            if problem is not None:
                raise HTTPException(status_code=422, detail=problem)

            # Every email carrying the same portal link, in any source account, is waiting
            # for the same document, and one upload settles them all.
            group = [
                w
                for w in waiting
                if w is target or (target.portal_link and w.portal_link == target.portal_link)
            ]
            identity = (
                content_hash(target.portal_link.encode()) if target.portal_link else file_hash
            )

            held = held_document(ledger, file_hash, identity)
            if held is not None:
                raise HTTPException(
                    status_code=409,
                    detail=f"The same billing document ({held.extraction.vendor}, "
                    f"{held.extraction.invoice_date.isoformat()}) is already held for review "
                    f"from {held.source_account}. Approve or reject it there first, then "
                    "upload this again.",
                )

            outcome: UploadOutcome
            known = known_document(ledger, file_hash, identity)
            if known is not None:
                extraction = known.extraction
                filed_month = CollectionMonth.of(extraction.invoice_date)
                inr = known.inr_rate if known.inr_rate is not None else rate(extraction)
                documents = (
                    CollectedDocument(
                        known.content_hash,
                        extraction,
                        known.file_link,
                        inr,
                        known.vendor_as_read,
                    ),
                )
                recorded_under, link, doubts = known.content_hash, known.file_link, ()
                outcome = "already_collected"
                for each in group:
                    ledger.record(
                        filed_month,
                        each.email,
                        EmailState.COLLECTED,
                        invoice_format=each.invoice_format,
                        portal_link=each.portal_link,
                        documents=documents,
                    )
            else:
                with tracing.scope(
                    collection_month=str(month),
                    source_account=target.email.source_account,
                    message_id=target.email.message_id,
                    invoice_format=InvoiceFormat.PORTAL_LINK,
                ):
                    reading = read(pdf, target.email, identity, steps)
                    extraction, as_read = as_expected(
                        ledger, reading.extraction, pdf, target.email, identity, steps
                    )
                # The invoice date decides the collection month, as it does in a run.
                filed_month = CollectionMonth.of(extraction.invoice_date)
                found = _history(ledger, filed_month, extraction, identity)
                results = history_checks(extraction, found, threshold)
                steps.checked(target.email, "history", results, identity)
                doubts = (*reading.doubts, *(d for result in results for d in result.doubts))
                inr = rate(extraction)
                recorded_under = identity
                if doubts:
                    link = save(f"{filed_month}/pending", extraction, pdf)
                    pending = PendingDocument(
                        identity,
                        extraction,
                        link,
                        doubts,
                        inr,
                        reading.read_again,
                        vendor_as_read=as_read,
                    )
                    outcome = "held"
                    for each in group:
                        ledger.record(
                            filed_month,
                            each.email,
                            EmailState.NEEDS_REVIEW,
                            reason=summary_of(doubts),
                            invoice_format=each.invoice_format,
                            portal_link=each.portal_link,
                            pending=(pending,),
                        )
                else:
                    link = save(str(filed_month), extraction, pdf)
                    collected = CollectedDocument(identity, extraction, link, inr, as_read)
                    outcome = "collected"
                    for each in group:
                        ledger.record(
                            filed_month,
                            each.email,
                            EmailState.COLLECTED,
                            invoice_format=each.invoice_format,
                            portal_link=each.portal_link,
                            documents=(collected,),
                        )

                # A later run that finds the same bytes attached to an email knows them as
                # this document, and does not file it as a second charge.
                ledger.record_alias(file_hash, identity)
                steps.add(target.email, trail.FILED, trail.RUN, trail.filed(link), identity)
                details = trail.converted(extraction, inr)
                steps.add(target.email, trail.CONVERTED, trail.RUN, details, identity)

            at = steps.at
            reading_steps = steps.events
            steps.events = []
            uploaded = {"size": len(pdf), "outcome": outcome, "file_name": file_name(link)}
            for each in group:
                steps.add(each.email, trail.UPLOADED, person, uploaded, recorded_under)
            # Who uploaded it comes first, then how it was read and checked, as for a run.
            steps.events.extend(reading_steps)
            for each in group:
                if outcome == "held":
                    details = trail.held(doubts)
                    steps.add(each.email, trail.HELD, trail.RUN, details, recorded_under)
                else:
                    steps.add(each.email, trail.COLLECTED, trail.RUN, {}, recorded_under)
            ledger.record_events(steps.events)
            records = [
                UploadRecord(
                    collection_month=str(filed_month),
                    source_account=each.email.source_account,
                    message_id=each.email.message_id,
                    subject=each.email.subject,
                    portal_link=each.portal_link,
                    file_hash=file_hash,
                    size=len(pdf),
                    content_hash=recorded_under,
                    file_name=file_name(link),
                    outcome=outcome,
                    document=_document_view(extraction, doubts),
                    person=person,
                    uploaded_at=at,
                )
                for each in group
            ]
            for record in records:
                history.record(record)
            return _view(records[0])

    @router.get("/uploads")
    def uploads(month: Month) -> list[AssistedDownload]:  # pyright: ignore[reportUnusedFunction]
        """Every upload filed or held under the month, newest first."""
        return [_view(record) for record in history.of_month(month)]

    @router.post("/{source_account}/{message_id}/upload")
    async def upload_pdf(  # pyright: ignore[reportUnusedFunction]
        month: Month,
        source_account: str,
        message_id: str,
        request: Request,
        person: Annotated[str, Depends(signed_in_person)],
    ) -> AssistedDownload:
        """Takes the PDF as the body of the request, in its own bytes."""
        pdf = await _body(request)
        return await run_in_threadpool(
            upload, CollectionMonth.parse(month), source_account, message_id, pdf, person
        )

    return router
