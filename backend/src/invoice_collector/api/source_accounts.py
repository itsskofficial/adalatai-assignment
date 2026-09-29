"""What the Source accounts screen shows and does.

A person connects a source account by granting read-only access at Google, and comes
back to the dashboard. Its sign-in is stored where invoice-collector-setup stores one,
so an account connected here is connected for the command line too, and the reverse.
Every connected source account is kept in the registry, which the next run reads.

Stored sign-ins are secrets: no route returns one, and none is logged.
"""

import hmac
import re
import secrets
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi import Path as PathParameter
from fastapi.responses import RedirectResponse
from google.auth.exceptions import GoogleAuthError
from pydantic import BaseModel

from invoice_collector import google_auth
from invoice_collector.api.sample_mail import (
    SampleMail,
    SampleMailInserter,
    SampleMailNotInserted,
    SampleMailUnavailable,
)
from invoice_collector.api.settings import Settings
from invoice_collector.api.source_account_connector import (
    INSERT_SCOPES,
    ConnectionNotCompleted,
    SourceAccountConnector,
)
from invoice_collector.api.vendor_history import VendorHistory
from invoice_collector.domain import CollectionMonth
from invoice_collector.google_auth import (
    DRIVE_FILE,
    GMAIL_INSERT,
    GMAIL_READONLY,
    NotSignedIn,
    SignInExpired,
)
from invoice_collector.ledger import Ledger
from invoice_collector.reconciler import vendor_key
from invoice_collector.source_account_registry import (
    OwnerAccountCannotBeRemoved,
    RegisteredSourceAccount,
    SourceAccountAction,
    SourceAccountRegistry,
    normalise,
)

# A sign-in that stops working within this long is marked as expiring soon.
EXPIRING_SOON = timedelta(days=2)
ADDRESS_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PENDING = "source_account_connecting"
RESULT = "source_account_connection_result"
SAMPLE_MAIL_RESULT = "sample_mail_result"
# What a pending trip to Google is for, when it is not connecting or renewing.
SAMPLE_MAIL = "sample_mail"

SignInState = Literal["works", "expired", "missing", "unknown"]
Outcome = Literal["connected", "renewed", "wrong_address", "failed"]


class ConnectRequest(BaseModel):
    address: str
    owner: bool = False


class AuthorizationUrl(BaseModel):
    """Where to send the person to grant access at Google."""

    authorization_url: str


class LatestRun(BaseModel):
    """The latest run that tried to read the source account."""

    month: str
    read: bool
    reason: str | None
    billing_documents: int


class SourceAccountView(BaseModel):
    address: str
    is_owner: bool
    connected_by: str
    connected_at: str
    sign_in: SignInState
    sign_in_problem: str | None
    # When the sign-in stops working, when that can be known.
    sign_in_ends_at: str | None
    expiring_soon: bool
    # The owner account's sign-in must also reach the Drive files the tool creates.
    needs_drive_access: bool
    last_read_month: str | None
    latest_run: LatestRun | None


class SourceAccountList(BaseModel):
    source_accounts: list[SourceAccountView]
    # Signed in from the command line on this machine, but not connected.
    found_on_this_machine: list[str]
    sign_in_lifetime_days: int | None


class ConnectionResult(BaseModel):
    """How the latest connection through Google ended, for the screen to say."""

    outcome: Outcome | None
    address: str | None = None
    signed_in_address: str | None = None
    reason: str | None = None


class AddedSourceAccount(BaseModel):
    address: str


class OwnerChanged(BaseModel):
    address: str
    needs_renewal: bool
    message: str


class SourceAccountChangeView(BaseModel):
    address: str
    action: SourceAccountAction
    person: str
    changed_at: str


class SampleMailboxView(BaseModel):
    name: str
    emails: int
    # The expected vendors that bill it, which may be added to the expected vendor list.
    vendors: list[str]


class SampleMailOffer(BaseModel):
    """Whether sample mail can be put into a connected source account, and what it holds."""

    available: bool
    # Why it is not available, when it is not.
    reason: str | None
    # Only administrators may put sample mail into a mailbox.
    can_fill: bool
    mailboxes: list[SampleMailboxView]
    # Where the portal links of the sample mail will lead, or None when left as generated.
    portal_url: str | None


class FillWithSampleMail(BaseModel):
    sample_mailbox: str
    # Also add the expected vendors that bill the sample mailbox, as billing this address.
    fill_expected_vendors: bool = False


class SampleMailResult(BaseModel):
    """What putting sample mail into a mailbox did, for the screen to say."""

    outcome: Literal["filled", "failed"] | None
    address: str | None = None
    sample_mailbox: str | None = None
    inserted: int = 0
    already_there: int = 0
    vendors_added: list[str] = []
    # Vendors of the sample mailbox already on the list, left as they are.
    vendors_already_listed: list[str] = []
    portal_url: str | None = None
    reason: str | None = None


class SampleMailStarted(BaseModel):
    """Either where to send the person at Google for leave to insert, or what was done."""

    authorization_url: str | None
    result: SampleMailResult | None


def scopes_for(owner: bool) -> list[str]:
    """Read-only Gmail, and for the owner account the Drive files the tool creates."""
    return [GMAIL_READONLY, DRIVE_FILE] if owner else [GMAIL_READONLY]


def check_sign_in(address: str, settings: Settings) -> tuple[SignInState, str | None]:
    """Whether the stored sign-in works, without opening a browser.

    A stored sign-in whose access is still current is not sent to Google at all; one
    whose access ran out is refreshed once, and the refreshed access lasts an hour.
    """
    try:
        google_auth.sign_in(address, [GMAIL_READONLY], settings.token_dir, allow_browser=False)
    except NotSignedIn:
        return "missing", None
    except SignInExpired:
        return "expired", None
    except (OSError, ValueError, GoogleAuthError):
        # The error itself is not shown: it may quote Google's answer.
        return "unknown", "The sign-in could not be checked: Google could not be reached"
    return "works", None


def sign_in_ends_at(
    account: RegisteredSourceAccount,
    stored: google_auth.StoredSignIn,
    lifetime_days: int | None,
) -> datetime | None:
    """When a sign-in issued through the dashboard stops working, for an app in testing.

    Unknown for a published app, and for a sign-in replaced from the command line.
    """
    if lifetime_days is None or account.signed_in_at is None:
        return None
    if account.sign_in_fingerprint != stored.fingerprint:
        return None
    return account.signed_in_at + timedelta(days=lifetime_days)


def _last_read_and_latest_run(ledger: Ledger, address: str) -> tuple[str | None, LatestRun | None]:
    last_read: str | None = None
    latest: LatestRun | None = None
    for month in reversed(ledger.months()):
        sync = next(
            (s for s in ledger.syncs(month) if s.source_account.casefold() == address), None
        )
        if sync is None:
            continue
        if latest is None:
            latest = LatestRun(
                month=str(month),
                read=sync.succeeded,
                reason=sync.reason,
                billing_documents=_documents_from(ledger, month, address),
            )
        if sync.succeeded:
            last_read = str(month)
            break
    return last_read, latest


def _documents_from(ledger: Ledger, month: CollectionMonth, address: str) -> int:
    return sum(1 for d in ledger.documents(month) if d.source_account.casefold() == address)


LedgerFactory = Callable[[], Ledger]
PersonDependency = Callable[..., str]
Address = Annotated[str, PathParameter(description="A source account's address")]


def source_account_routes(
    settings: Settings,
    ledger_factory: LedgerFactory,
    connector: SourceAccountConnector | None,
    signed_in_person: PersonDependency,
    now: Callable[[], datetime],
    *,
    role_of: Callable[[str], str | None] = lambda person: None,
    sample_mail: SampleMail | None = None,
    inserter: SampleMailInserter | None = None,
) -> tuple[APIRouter, APIRouter]:
    """The screen's API routes, and the route Google sends the person back to.

    With the sample mail and something to insert it, administrators may put the sample
    mail into a connected source account's mailbox.
    """
    registry = SourceAccountRegistry(settings.ledger_path)
    vendor_history = VendorHistory(settings.ledger_path)
    router = APIRouter(prefix="/source-accounts")
    callback_router = APIRouter()
    screen = f"{settings.frontend_origin}/source-accounts"

    def administrator(person: Annotated[str, Depends(signed_in_person)]) -> str:
        if role_of(person) != "administrator":
            raise HTTPException(
                status_code=403,
                detail="Only an administrator can put sample mail into a mailbox",
            )
        return person

    @contextmanager
    def opened() -> Generator[Ledger]:
        ledger = ledger_factory()
        try:
            yield ledger
        finally:
            ledger.close()

    def registered(address: str) -> RegisteredSourceAccount:
        account = registry.find(address)
        if account is None:
            raise HTTPException(
                status_code=404, detail=f"{address} is not a connected source account"
            )
        return account

    def send_to_google(
        request: Request, person: str, address: str, *, owner: bool, renew: bool
    ) -> AuthorizationUrl:
        if connector is None:
            raise HTTPException(
                status_code=503, detail="Connecting source accounts is unavailable just now"
            )
        state = secrets.token_urlsafe(32)
        # Bound to this person's session and to the address being connected.
        request.session[PENDING] = {
            "state": state,
            "address": address,
            "owner": owner,
            "renew": renew,
            "person": person,
        }
        return AuthorizationUrl(
            authorization_url=connector.authorization_url(state, address, scopes_for(owner))
        )

    def view(account: RegisteredSourceAccount, ledger: Ledger, at: datetime) -> SourceAccountView:
        stored = google_auth.stored_sign_in(account.address, settings.token_dir)
        ends_at: datetime | None = None
        problem: str | None = None
        if stored is None:
            state: SignInState = "missing"
        else:
            ends_at = sign_in_ends_at(account, stored, settings.sign_in_lifetime_days)
            if ends_at is not None and ends_at <= at:
                # Google has let the refresh token lapse: no need to ask it.
                state = "expired"
            else:
                state, problem = check_sign_in(account.address, settings)
        last_read, latest_run = _last_read_and_latest_run(ledger, account.address)
        return SourceAccountView(
            address=account.address,
            is_owner=account.is_owner,
            connected_by=account.connected_by,
            connected_at=account.connected_at.isoformat(),
            sign_in=state,
            sign_in_problem=problem,
            sign_in_ends_at=ends_at.isoformat() if ends_at is not None else None,
            expiring_soon=state == "works"
            and ends_at is not None
            and ends_at - at <= EXPIRING_SOON,
            needs_drive_access=account.is_owner
            and (stored is None or DRIVE_FILE not in stored.scopes),
            last_read_month=last_read,
            latest_run=latest_run,
        )

    @router.get("")
    def source_accounts() -> SourceAccountList:  # pyright: ignore[reportUnusedFunction]
        accounts = registry.accounts()
        at = now()
        with opened() as ledger:
            views = [view(account, ledger, at) for account in accounts]
        connected = {account.address for account in accounts}
        found = [
            each.account
            for each in google_auth.stored_sign_ins(settings.token_dir)
            if normalise(each.account) not in connected
        ]
        return SourceAccountList(
            source_accounts=views,
            found_on_this_machine=found,
            sign_in_lifetime_days=settings.sign_in_lifetime_days,
        )

    @router.get("/history")
    def history() -> list[SourceAccountChangeView]:  # pyright: ignore[reportUnusedFunction]
        return [
            SourceAccountChangeView(
                address=change.address,
                action=change.action,
                person=change.person,
                changed_at=change.changed_at.isoformat(),
            )
            for change in registry.history()
        ]

    @router.get("/connection-result")
    def connection_result(request: Request) -> ConnectionResult:  # pyright: ignore[reportUnusedFunction]
        kept = request.session.get(RESULT)
        if not isinstance(kept, dict):
            return ConnectionResult(outcome=None)
        return ConnectionResult.model_validate(kept)

    @router.get("/sample-mail")
    def sample_mail_offer(  # pyright: ignore[reportUnusedFunction]
        person: Annotated[str, Depends(signed_in_person)],
    ) -> SampleMailOffer:
        can_fill = role_of(person) == "administrator"
        if sample_mail is None or inserter is None:
            return SampleMailOffer(
                available=False,
                reason="There is no sample mail beside this dashboard.",
                can_fill=can_fill,
                mailboxes=[],
                portal_url=None,
            )
        try:
            mailboxes = sample_mail.mailboxes()
        except SampleMailUnavailable as problem:
            return SampleMailOffer(
                available=False,
                reason=str(problem),
                can_fill=can_fill,
                mailboxes=[],
                portal_url=None,
            )
        return SampleMailOffer(
            available=bool(mailboxes),
            reason=None if mailboxes else "The sample mail holds no mailbox.",
            can_fill=can_fill,
            mailboxes=[
                SampleMailboxView(name=m.name, emails=m.emails, vendors=list(m.vendors))
                for m in mailboxes
            ],
            portal_url=sample_mail.portal_url,
        )

    @router.get("/sample-mail-result")
    def sample_mail_result(request: Request) -> SampleMailResult:  # pyright: ignore[reportUnusedFunction]
        kept = request.session.get(SAMPLE_MAIL_RESULT)
        if not isinstance(kept, dict):
            return SampleMailResult(outcome=None)
        return SampleMailResult.model_validate(kept)

    def fill(address: str, mailbox: str, with_vendors: bool, person: str) -> SampleMailResult:
        """Puts the sample mailbox's emails into the mailbox at address, and, when asked, its
        expected vendors on the list. Raises SampleMailNotInserted or SampleMailUnavailable."""
        assert sample_mail is not None and inserter is not None
        report = inserter.insert(address, sample_mail.messages_for(mailbox, address))
        at = now()
        added: list[str] = []
        listed: list[str] = []
        if with_vendors:
            with opened() as ledger:
                known = {vendor_key(v.vendor) for v in ledger.expected_vendors()}
                for vendor in sample_mail.vendors_for(mailbox, address):
                    # A vendor already on the list, whatever a person decided of it, stays.
                    if vendor_key(vendor.vendor) in known:
                        listed.append(vendor.vendor)
                        continue
                    ledger.save_expected_vendor(vendor)
                    vendor_history.record("added", person, at, None, vendor)
                    known.add(vendor_key(vendor.vendor))
                    added.append(vendor.vendor)
        registry.filled_with_sample_mail(address, person, at)
        return SampleMailResult(
            outcome="filled",
            address=address,
            sample_mailbox=mailbox,
            inserted=len(report.inserted),
            already_there=len(report.skipped),
            vendors_added=added,
            vendors_already_listed=listed,
            portal_url=sample_mail.portal_url,
        )

    @router.post("/{address}/sample-mail")
    def fill_with_sample_mail(  # pyright: ignore[reportUnusedFunction]
        address: Address,
        asked: FillWithSampleMail,
        request: Request,
        person: Annotated[str, Depends(administrator)],
    ) -> SampleMailStarted:
        account = registered(address)
        if sample_mail is None or inserter is None:
            raise HTTPException(
                status_code=503, detail="There is no sample mail beside this dashboard."
            )
        try:
            names = [m.name for m in sample_mail.mailboxes()]
        except SampleMailUnavailable as problem:
            raise HTTPException(status_code=503, detail=str(problem)) from None
        if asked.sample_mailbox not in names:
            raise HTTPException(
                status_code=422,
                detail=f"{asked.sample_mailbox} is not a sample mailbox. Choose one of: "
                f"{', '.join(names)}.",
            )
        if google_auth.stored_sign_in(account.address, settings.token_dir) is None:
            raise HTTPException(
                status_code=409,
                detail=f"{account.address} has no working sign-in for reading, which is needed "
                "to find what the mailbox already holds. Renew it first.",
            )
        if inserter.can_insert(account.address):
            try:
                result = fill(
                    account.address, asked.sample_mailbox, asked.fill_expected_vendors, person
                )
            except (SampleMailNotInserted, SampleMailUnavailable) as problem:
                raise HTTPException(status_code=502, detail=str(problem)) from None
            return SampleMailStarted(authorization_url=None, result=result)
        if connector is None:
            raise HTTPException(
                status_code=503, detail="Asking Google for leave to insert is unavailable just now"
            )
        state = secrets.token_urlsafe(32)
        request.session[PENDING] = {
            "state": state,
            "address": account.address,
            "person": person,
            "purpose": SAMPLE_MAIL,
            "sample_mailbox": asked.sample_mailbox,
            "fill_expected_vendors": asked.fill_expected_vendors,
        }
        return SampleMailStarted(
            authorization_url=connector.authorization_url(state, account.address, INSERT_SCOPES),
            result=None,
        )

    @router.post("/connect")
    def connect(  # pyright: ignore[reportUnusedFunction]
        asked: ConnectRequest,
        request: Request,
        person: Annotated[str, Depends(signed_in_person)],
    ) -> AuthorizationUrl:
        address = normalise(asked.address)
        if not ADDRESS_PATTERN.match(address):
            raise HTTPException(status_code=422, detail="Give the email address of the mailbox")
        if registry.find(address) is not None:
            raise HTTPException(
                status_code=409,
                detail=f"{address} is already connected. Renew its sign-in instead.",
            )
        return send_to_google(request, person, address, owner=asked.owner, renew=False)

    @router.post("/{address}/renew")
    def renew(  # pyright: ignore[reportUnusedFunction]
        address: Address,
        request: Request,
        person: Annotated[str, Depends(signed_in_person)],
    ) -> AuthorizationUrl:
        account = registered(address)
        return send_to_google(request, person, account.address, owner=account.is_owner, renew=True)

    @router.post("/{address}/add", status_code=201)
    def add(  # pyright: ignore[reportUnusedFunction]
        address: Address,
        person: Annotated[str, Depends(signed_in_person)],
    ) -> AddedSourceAccount:
        address = normalise(address)
        if registry.find(address) is not None:
            raise HTTPException(status_code=409, detail=f"{address} is already connected")
        if google_auth.stored_sign_in(address, settings.token_dir) is None:
            raise HTTPException(
                status_code=404, detail=f"No sign-in for {address} is stored on this machine"
            )
        # When a sign-in made from the command line was issued is not known.
        registry.signed_in(address, "added", person, now(), signed_in_at=None, fingerprint=None)
        return AddedSourceAccount(address=address)

    @router.post("/{address}/make-owner")
    def make_owner(  # pyright: ignore[reportUnusedFunction]
        address: Address,
        person: Annotated[str, Depends(signed_in_person)],
    ) -> OwnerChanged:
        account = registered(address)
        registry.make_owner(account.address, person, now())
        stored = google_auth.stored_sign_in(account.address, settings.token_dir)
        needs_renewal = stored is None or DRIVE_FILE not in stored.scopes
        message = (
            f"{account.address} is now the owner account. Renew its sign-in to give it "
            "access to the Drive files the tool creates; until then a run cannot archive "
            "to Drive or write the summary there."
            if needs_renewal
            else f"{account.address} is now the owner account. Its sign-in already reaches "
            "the Drive files the tool creates."
        )
        return OwnerChanged(address=account.address, needs_renewal=needs_renewal, message=message)

    @router.delete("/{address}", status_code=204)
    def remove(  # pyright: ignore[reportUnusedFunction]
        address: Address,
        person: Annotated[str, Depends(signed_in_person)],
    ) -> Response:
        account = registered(address)
        try:
            registry.remove(account.address, person, now())
        except OwnerAccountCannotBeRemoved:
            raise HTTPException(
                status_code=409,
                detail=f"{account.address} is the owner account. Make another source account "
                "the owner before removing it.",
            ) from None
        # What was collected from it stays in the ledger.
        google_auth.forget_sign_in(account.address, settings.token_dir)
        return Response(status_code=204)

    def finish(request: Request, result: ConnectionResult) -> RedirectResponse:
        request.session[RESULT] = result.model_dump()
        slug = (result.outcome or "failed").replace("_", "-")
        return RedirectResponse(f"{screen}?connect={slug}", status_code=302)

    def failed(request: Request, reason: str, address: str | None = None) -> RedirectResponse:
        return finish(request, ConnectionResult(outcome="failed", address=address, reason=reason))

    def sample_mail_done(request: Request, result: SampleMailResult) -> RedirectResponse:
        request.session[SAMPLE_MAIL_RESULT] = result.model_dump()
        return RedirectResponse(f"{screen}?sample-mail={result.outcome}", status_code=302)

    def sample_mail_callback(
        request: Request, started: dict[str, Any], person: str, code: str, error: str
    ) -> RedirectResponse:
        """Google's answer to asking for leave to insert: store it apart, then insert."""
        address = str(started.get("address", ""))
        mailbox = str(started.get("sample_mailbox", ""))

        def not_filled(reason: str) -> RedirectResponse:
            return sample_mail_done(
                request,
                SampleMailResult(
                    outcome="failed", address=address, sample_mailbox=mailbox, reason=reason
                ),
            )

        assert connector is not None
        if role_of(person) != "administrator":
            return not_filled("Only an administrator can put sample mail into a mailbox.")
        if sample_mail is None or inserter is None:
            return not_filled("There is no sample mail beside this dashboard.")
        if error or not code:
            return not_filled("Leave to insert mail was not granted at Google. Nothing was put in.")
        try:
            signed_in = connector.signed_in_to_insert(code)
        except ConnectionNotCompleted as problem:
            return not_filled(f"{problem}. Nothing was put in.")
        if normalise(signed_in.address) != address:
            return not_filled(
                f"{signed_in.address} signed in at Google, not {address}. Nothing was stored "
                f"and nothing was put in. Try again, and choose {address} at Google."
            )
        granted = set(cast(Sequence[str], signed_in.credentials.scopes or ()))  # pyright: ignore[reportUnknownMemberType]
        if GMAIL_INSERT not in granted:
            return not_filled("Leave to insert mail was not granted. Tick the box Google shows.")
        # Stored apart from the reading sign-in, which is never widened.
        google_auth.store_sign_in(
            address, signed_in.credentials, settings.token_dir, purpose=google_auth.SEEDING
        )
        try:
            result = fill(address, mailbox, bool(started.get("fill_expected_vendors")), person)
        except (SampleMailNotInserted, SampleMailUnavailable) as problem:
            return not_filled(str(problem))
        return sample_mail_done(request, result)

    @callback_router.get("/accounts/callback")
    def accounts_callback(  # pyright: ignore[reportUnusedFunction]
        request: Request, state: str = "", code: str = "", error: str = ""
    ) -> RedirectResponse:
        pending: Any = request.session.pop(PENDING, None)
        # The same check as every other route, so a person removed from the list while
        # at Google cannot finish connecting.
        try:
            person: str | None = signed_in_person(request)
        except HTTPException:
            person = None
        if connector is None or not isinstance(pending, dict) or person is None:
            return failed(request, "No connection was in progress. Start it again.")
        started = cast(dict[str, Any], pending)
        address = str(started.get("address", ""))
        expected = started.get("state")
        if (
            started.get("person") != person
            or not isinstance(expected, str)
            or not expected
            or not hmac.compare_digest(expected.encode(), state.encode())
        ):
            return failed(request, "The answer from Google did not match this session.", address)
        if started.get("purpose") == SAMPLE_MAIL:
            return sample_mail_callback(request, started, person, code, error)
        if error or not code:
            return failed(request, "Access was not granted at Google", address)
        owner = bool(started.get("owner"))
        scopes = scopes_for(owner)
        try:
            signed_in = connector.signed_in(code, scopes)
        except ConnectionNotCompleted as problem:
            return failed(request, str(problem), address)
        if normalise(signed_in.address) != address:
            return finish(
                request,
                ConnectionResult(
                    outcome="wrong_address", address=address, signed_in_address=signed_in.address
                ),
            )
        granted = set(cast(Sequence[str], signed_in.credentials.scopes or ()))  # pyright: ignore[reportUnknownMemberType]
        if not set(scopes) <= granted:
            return failed(
                request,
                "Not all the access asked for was granted. Tick every box Google shows.",
                address,
            )

        google_auth.store_sign_in(address, signed_in.credentials, settings.token_dir)
        stored = google_auth.stored_sign_in(address, settings.token_dir)
        renewed = bool(started.get("renew")) and registry.find(address) is not None
        at = now()
        registry.signed_in(
            address,
            "renewed" if renewed else "connected",
            person,
            at,
            signed_in_at=at,
            fingerprint=stored.fingerprint if stored is not None else None,
            owner=owner,
        )
        return finish(
            request,
            ConnectionResult(
                outcome="renewed" if renewed else "connected",
                address=address,
                signed_in_address=signed_in.address,
            ),
        )

    return router, callback_router
