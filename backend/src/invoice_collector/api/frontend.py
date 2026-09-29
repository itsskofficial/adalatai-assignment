"""The built front end, served by the dashboard service so that pages and API share one origin.

The front end chooses its screen from the address, so every address that is not the API,
sign-in or a built file is answered with the page. A built file that is not there is
missing, not a screen, and is answered as missing. Nothing outside the folder is served.
"""

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

PAGE = "index.html"


def frontend_routes(folder: Path) -> APIRouter:
    """Routes serving the folder `npm run build` writes. Include them after every other route."""
    root = folder.resolve()
    router = APIRouter()

    @router.get("/{path:path}", include_in_schema=False)
    def built(path: str) -> FileResponse:  # pyright: ignore[reportUnusedFunction]
        wanted = (root / path).resolve()
        inside = wanted.is_relative_to(root)
        if inside and wanted.is_file():
            return FileResponse(wanted)
        # Screens have no file extension; anything with one was asked for as a file.
        if not inside or "." in path.rsplit("/", 1)[-1]:
            raise HTTPException(status_code=404, detail="Not found")
        # Never kept by the browser, so a new build is picked up at the next visit.
        return FileResponse(root / PAGE, headers={"Cache-Control": "no-cache"})

    return router
