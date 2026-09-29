"""Where PDFs are kept."""

from pathlib import Path
from typing import Protocol


class Archive(Protocol):
    def save(self, folder: str, filename: str, pdf: bytes) -> str:
        """Stores the PDF and returns a link to it."""
        ...


class LocalArchive:
    def __init__(self, root: Path) -> None:
        self._root = root

    def save(self, folder: str, filename: str, pdf: bytes) -> str:
        """The link is relative to the folder that holds the archive, so output can be moved."""
        target = self._root / folder / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(pdf)
        return target.relative_to(self._root.parent).as_posix()
