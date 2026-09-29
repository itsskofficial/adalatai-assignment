"""The dashboard's API, built by a factory so tests can pass fakes."""

import hmac
import secrets
from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi import Path as PathParameter
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from invoice_collector.api.identity import IdentityNotVerified, IdentityVerifier
from invoice_collector.api.month_summary import MonthSummary, filed_document, month_summary
from invoice_collector.api.months import collection_months
from invoice_collector.api.settings import Settings, normalise
from invoice_collector.api.spend import Spend, months_in_range, spend, spend_of_nothing
from invoice_collector.domain import CollectionMonth
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


def create_app(
    settings: Settings, ledger_factory: LedgerFactory, identity_verifier: IdentityVerifier
) -> FastAPI:
    settings.check()
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
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )

    def signed_in_person(request: Request) -> str:
        email = request.session.get("email")
        if not isinstance(email, str) or not settings.allows(email):
            raise HTTPException(status_code=401, detail="Sign in to use the dashboard")
        return email

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
        if not settings.allows(email):
            return back_to_dashboard("refused")
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
        return {"email": person}

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

    @api.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    def nothing_here(path: str) -> None:  # pyright: ignore[reportUnusedFunction]
        raise HTTPException(status_code=404, detail="Not found")

    app.include_router(api)
    return app
