"""Keeps PDFs in Google Drive, one folder per collection month under a root folder.

Only the files this tool creates are reachable, through the drive.file scope.
"""

import hashlib
from pathlib import PurePosixPath
from typing import Any

from googleapiclient.http import MediaInMemoryUpload

from invoice_collector.google_auth import DRIVE_FILE

SCOPES = (DRIVE_FILE,)
DEFAULT_ROOT_FOLDER = "Invoice Collection"
FOLDER = "application/vnd.google-apps.folder"
PDF = "application/pdf"
_TOP_OF_MY_DRIVE = "root"
_RETRIES = 3


def quoted(text: str) -> str:
    """The text as a quoted string for a Drive search query."""
    escaped = text.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


class DriveFolders:
    """The root folder and the folders inside it, found or created, their ids remembered.

    service is a Drive v3 client acting as the owner account.
    """

    def __init__(self, service: Any, root_folder: str = DEFAULT_ROOT_FOLDER) -> None:
        self._service = service
        self._root_folder = root_folder
        self._ids: dict[str, str] = {}

    def folder_id(self, path: str = "") -> str:
        """The id of the folder at path under the root folder; "" is the root folder."""
        key = path.strip("/")
        if key not in self._ids:
            if key:
                parent, _, name = key.rpartition("/")
                self._ids[key] = self._find_or_create(name, self.folder_id(parent))
            else:
                self._ids[key] = self._find_or_create(self._root_folder, _TOP_OF_MY_DRIVE)
        return self._ids[key]

    def find(
        self, name: str, parent: str, mime_type: str, fields: str = "id"
    ) -> list[dict[str, Any]]:
        """The files of that name and type in the parent folder, not in the bin."""
        query = (
            f"name = {quoted(name)} and {quoted(parent)} in parents "
            f"and mimeType = {quoted(mime_type)} and trashed = false"
        )
        found: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            page: dict[str, Any] = (
                self._service.files()
                .list(
                    q=query,
                    spaces="drive",
                    fields=f"nextPageToken, files({fields})",
                    pageToken=page_token,
                )
                .execute(num_retries=_RETRIES)
            )
            found.extend(page.get("files", []))
            page_token = page.get("nextPageToken")
            if not page_token:
                return found

    def create(
        self, name: str, parent: str, mime_type: str, media: Any = None, fields: str = "id"
    ) -> dict[str, Any]:
        body = {"name": name, "parents": [parent], "mimeType": mime_type}
        created: dict[str, Any] = (
            self._service.files()
            .create(body=body, media_body=media, fields=fields)
            .execute(num_retries=_RETRIES)
        )
        return created

    def _find_or_create(self, name: str, parent: str) -> str:
        existing = self.find(name, parent, FOLDER)
        if existing:
            return str(existing[0]["id"])
        return str(self.create(name, parent, FOLDER)["id"])


class DriveArchive:
    """Never overwrites a different document, the same as the local archive.

    A document already saved under the name is reused and nothing is uploaded.
    A different document under the same name gets a numbered suffix.
    The link is the document's page in Drive.
    """

    def __init__(self, service: Any, root_folder: str = DEFAULT_ROOT_FOLDER) -> None:
        self._folders = DriveFolders(service, root_folder)

    def save(self, folder: str, filename: str, pdf: bytes) -> str:
        parent = self._folders.folder_id(folder)
        checksum = hashlib.md5(pdf, usedforsecurity=False).hexdigest()
        name = PurePosixPath(filename)
        candidate, number = filename, 2
        while existing := self._folders.find(
            candidate, parent, PDF, fields="id, md5Checksum, webViewLink"
        ):
            for file in existing:
                if file.get("md5Checksum") == checksum:
                    return str(file["webViewLink"])
            candidate = f"{name.stem}_{number}{name.suffix}"
            number += 1
        media = MediaInMemoryUpload(pdf, mimetype=PDF, resumable=False)
        uploaded = self._folders.create(candidate, parent, PDF, media, fields="id, webViewLink")
        return str(uploaded["webViewLink"])
