"""The dashboard's API, built by a factory so tests can pass fakes."""

import hmac
import secrets
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Annotated

import anthropic
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi import Path as PathParameter
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from invoice_collector.api.identity import IdentityNotVerified, IdentityVerifier
from invoice_collector.api.month_summary import MonthSummary, filed_document, month_summary
from invoice_collector.api.months import collection_months
from invoice_collector.api.people import People, Role
from invoice_collector.api.people_routes import people_routes
from invoice_collector.api.questions import (
    UNANSWERED_LOG,
    Answer,
    Answerer,
    Ledgered,
    Question,
    QuestionsUnavailable,
)
from invoice_collector.api.review import review_routes
from invoice_collector.api.settings import ALLOWLIST_VARIABLE, Settings, SettingsError, normalise
from invoice_collector.api.source_account_connector import SourceAccountConnector
from invoice_collector.api.source_accounts import source_account_routes
from invoice_collector.api.spend import Spend, months_in_range, spend, spend_of_nothing
from invoice_collector.api.vendor_history import VendorHistory
from invoice_collector.api.vendors import vendor_routes
from invoice_collector.charge_history import charges_in
from invoice_collector.domain import CollectionMonth
from invoice_collector.exchange_rates import ExchangeRates, NoExchangeRates
from invoice_collector.ledger import Ledger

LedgerFactory = Callable[[], Ledger]

SESSION_COOKIE = "invoice_collector_session"
SESSION_SECONDS = 12 * 60 * 60
MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"

Month = Annotated[
    str,
    PathParameter(pattern=MONTH_PATTERN, description="Collection month, YYYY-MM"),
]
FirstMonth = Annotated[
    str | None,
    Query(alias="from", pattern=MONTH_PATTERN, description="First collection month, YYYY-MM"),
]
LastMonth = Annotated[
    str | None,
    Query(alias="to", pattern=MONTH_PATTERN, description="Last collection month, YYYY-MM"),
]


NO_API_KEY = (
    "ANTHROPIC_API_KEY is not set, so Ask your invoices is unavailable. Set it and restart "
    "the dashboard; everything else works without it."
)


def create_app(
    settings: Settings,
    ledger_factory: LedgerFactory,
    identity_verifier: IdentityVerifier,
    *,
    claude: anthropic.Anthropic | None = None,
    source_account_connector: SourceAccountConnector | None = None,
    exchange_rates: ExchangeRates | None = None,
    today: Callable[[], date] = date.today,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> FastAPI:
    """The dashboard's API.

    Without a Claude client, only Ask your invoices is unavailable; without a source
    account connector, only connecting and renewing source accounts is.

    Exchange rates value a billing document approved on the Review screen in rupees.
    Without them, only rupee amounts are left empty.
    """
    settings.check()
    people = People(settings.ledger_path, settings.allowlist)
    if people.nobody_could_sign_in():
        raise SettingsError(
            f"{ALLOWLIST_VARIABLE} is empty and nobody is on the people list, so nobody could "
            "sign in. Set it to the address of at least one administrator."
        )
    answerer = (
        Answerer(claude, settings.ledger_path.parent / UNANSWERED_LOG, today)
        if claude is not None
        else None
    )
    app = FastAPI(title="Invoice Collection dashboard", docs_url=None, redoc_url=None)

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        session_cookie=SESSION_COOKIE,
        max_age=SESSION_SECONDS,
        same_site="lax",
        https_only=settings.redirect_uri.startswith("https://"),
    )
    # Added last so it runs first: a 401 must still carry the headers the browser needs.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.frontend_origin],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Content-Type"],
    )

    def signed_in_person(request: Request) -> str:
        email = request.session.get("email")
        # Checked on every request, so a person removed from the list is refused at once.
        if not isinstance(email, str) or people.role_of(email) is None:
            raise HTTPException(status_code=401, detail="Sign in to use the dashboard")
        return email

    def administrator(person: Annotated[str, Depends(signed_in_person)]) -> str:
        if people.role_of(person) != "administrator":
            raise HTTPException(
                status_code=403, detail="Only an administrator can manage who may sign in"
            )
        return person

    def back_to_dashboard(sign_in: str | None = None) -> RedirectResponse:
        query = f"?sign_in={sign_in}" if sign_in else ""
        return RedirectResponse(f"{settings.frontend_origin}/{query}", status_code=302)

    @app.get("/auth/login")
    def login(request: Request) -> RedirectResponse:  # pyright: ignore[reportUnusedFunction]
        state = secrets.token_urlsafe(32)
        request.session.clear()
        request.session["state"] = state
        return RedirectResponse(identity_verifier.authorization_url(state), status_code=302)

    @app.get("/auth/callback")
    def callback(  # pyright: ignore[reportUnusedFunction]
        request: Request, state: str = "", code: str = ""
    ) -> RedirectResponse:
        expected = request.session.pop("state", None)
        request.session.clear()
        if (
            not isinstance(expected, str)
            or not expected
            or not code
            or not hmac.compare_digest(expected.encode(), state.encode())
        ):
            return back_to_dashboard("failed")
        try:
            email = identity_verifier.verified_email(code)
        except IdentityNotVerified:
            return back_to_dashboard("failed")
        if people.role_of(email) is None:
            people.record_refusal(email, now())
            return back_to_dashboard("refused")
        people.record_sign_in(email, now())
        request.session["email"] = normalise(email)
        return back_to_dashboard()

    @app.post("/auth/logout", status_code=204)
    def logout(request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        request.session.clear()
        return Response(status_code=204)

    api = APIRouter(prefix="/api", dependencies=[Depends(signed_in_person)])

    @api.get("/me")
    def me(  # pyright: ignore[reportUnusedFunction]
        person: Annotated[str, Depends(signed_in_person)],
    ) -> dict[str, str]:
        role: Role | None = people.role_of(person)
        return {"email": person, "role": role or "member"}

    @api.get("/months")
    def months() -> dict[str, list[str]]:  # pyright: ignore[reportUnusedFunction]
        return {"months": collection_months(settings.ledger_path)}

    # The ledger is opened and closed inside each route: SQLite keeps a connection to the
    # thread that opened it, and a dependency may run on a different thread from its route.
    @api.get("/months/{month}/summary")
    def summary(month: Month) -> MonthSummary:  # pyright: ignore[reportUnusedFunction]
        ledger = ledger_factory()
        try:
            return month_summary(ledger, CollectionMonth.parse(month))
        finally:
            ledger.close()

    @api.get("/months/{month}/billing-documents/{file_name}")
    def billing_document(  # pyright: ignore[reportUnusedFunction]
        month: Month, file_name: str
    ) -> FileResponse:
        ledger = ledger_factory()
        try:
            path = filed_document(
                ledger, CollectionMonth.parse(month), file_name, settings.ledger_path.parent
            )
        finally:
            ledger.close()
        if path is None:
            raise HTTPException(status_code=404, detail="No such billing document")
        return FileResponse(path, media_type="application/pdf", content_disposition_type="inline")

    @api.get("/spend")
    def spend_in_rupees(  # pyright: ignore[reportUnusedFunction]
        first: FirstMonth = None, last: LastMonth = None
    ) -> Spend:
        if first is not None and last is not None and first > last:
            raise HTTPException(status_code=422, detail="The range starts after it ends")
        months = months_in_range(collection_months(settings.ledger_path), first, last)
        if not months:
            return spend_of_nothing()
        ledger = ledger_factory()
        try:
            return spend(ledger, months)
        finally:
            ledger.close()

    @api.post("/questions")
    def ask(asked: Question) -> Answer:  # pyright: ignore[reportUnusedFunction]
        if answerer is None:
            raise HTTPException(status_code=503, detail=NO_API_KEY)
        months = collection_months(settings.ledger_path)
        charges = []
        if months:
            ledger = ledger_factory()
            try:
                charges = charges_in(ledger, [CollectionMonth.parse(m) for m in months])
            finally:
                ledger.close()
        try:
            return answerer.answer(asked.question.strip(), Ledgered(charges, months))
        except QuestionsUnavailable as problem:
            raise HTTPException(status_code=503, detail=str(problem)) from None

    api.include_router(
        vendor_routes(ledger_factory, VendorHistory(settings.ledger_path), signed_in_person, now)
    )

    api.include_router(people_routes(people, administrator, now))
    source_accounts, accounts_callback = source_account_routes(
        settings, ledger_factory, source_account_connector, signed_in_person, now
    )
    api.include_router(source_accounts)
    app.include_router(accounts_callback)
    api.include_router(
        review_routes(
            ledger_factory,
            settings.ledger_path,
            exchange_rates or NoExchangeRates(),
            signed_in_person,
            now,
        )
    )

    @api.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    def nothing_here(path: str) -> None:  # pyright: ignore[reportUnusedFunction]
        raise HTTPException(status_code=404, detail="Not found")

    app.include_router(api)
    return app
