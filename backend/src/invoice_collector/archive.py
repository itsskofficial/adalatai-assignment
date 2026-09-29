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
        """Never overwrites a different document.

        A document already saved under the name is left as it is, so saving is repeatable.
        A different document under the same name gets a numbered suffix.
        The link is relative to the folder that holds the archive, so output can be moved.
        """
        directory = self._root / folder
        directory.mkdir(parents=True, exist_ok=True)
        target = self._free_path(directory, Path(filename), pdf)
        if not target.exists():
            target.write_bytes(pdf)
        return target.relative_to(self._root.parent).as_posix()

    @staticmethod
    def _free_path(directory: Path, name: Path, pdf: bytes) -> Path:
        candidate = directory / name
        number = 2
        while candidate.exists() and candidate.read_bytes() != pdf:
            candidate = directory / f"{name.stem}_{number}{name.suffix}"
            number += 1
        return candidate
