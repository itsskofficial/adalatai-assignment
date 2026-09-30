"""Refuses a change of state asked for by a page of another site.

The session cookie is SameSite=Lax, so a browser does not send it with a form another site
posts, and every change takes JSON. This is the second line: a request that changes
something must come from the dashboard's own pages. A browser says where a request comes
from in its Origin header, and in Sec-Fetch-Site; either naming another site is refused. A
request that carries neither did not come from a page in a browser, so it carries no
person's cookie unless that person sent it on purpose, and is left to the routes' own
checks.
"""

from collections.abc import Awaitable, Callable, Collection

from fastapi import Request, Response
from fastapi.responses import JSONResponse

CHANGES = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# The values of Sec-Fetch-Site for a request from the dashboard's own pages, or typed by
# the person. "same-site" is another origin on the same domain, and is refused.
OWN = frozenset({"same-origin", "none"})

CallNext = Callable[[Request], Awaitable[Response]]


def from_another_site(request: Request, own_origins: Collection[str]) -> bool:
    if request.method not in CHANGES:
        return False
    fetched_from = request.headers.get("sec-fetch-site")
    if fetched_from is not None and fetched_from.lower() not in OWN:
        return True
    origin = request.headers.get("origin")
    return origin is not None and origin.rstrip("/") not in own_origins


def same_origin_only(
    own_origins: Collection[str],
) -> Callable[[Request, CallNext], Awaitable[Response]]:
    """Middleware answering 403 to a change asked for from another site."""

    async def check(request: Request, call_next: CallNext) -> Response:
        if from_another_site(request, own_origins):
            return JSONResponse(
                {"detail": "Changes are made from the dashboard's own pages only"},
                status_code=403,
            )
        return await call_next(request)

    return check
