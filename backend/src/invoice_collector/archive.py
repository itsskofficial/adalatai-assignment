"""Where PDFs are kept."""

import hashlib
import re
from pathlib import Path, PurePosixPath
from typing import Protocol


class Archive(Protocol):
    def save(self, folder: str, filename: str, pdf: bytes) -> str:
        """Stores the PDF and returns a link to it."""
        ...

    def remove(self, folder: str, filename: str, pdf: bytes) -> None:
        """Removes the copy of this PDF that save stored under the name, if there is one.

        A different document under the name is left as it is.
        """
        ...


def is_named(name: str, filename: str) -> bool:
    """Whether a file is saved under the name, or under the name with a numbered suffix."""
    wanted = PurePosixPath(filename)
    pattern = rf"{re.escape(wanted.stem)}(_\d+)?{re.escape(wanted.suffix)}"
    return re.fullmatch(pattern, name) is not None


def pdf_sha256(pdf: bytes) -> str:
    """The SHA-256 of the PDF as saved, which tells its copy from others of the same name.

    It is not the document's identity. A document made from an email body or a portal page
    is known by that source, and its PDF differs each time it is made. See ADR 0013.
    """
    return hashlib.sha256(pdf).hexdigest()


class BothArchives:
    """Saves each PDF to two archives, and gives the link from the first."""

    def __init__(self, first: Archive, second: Archive) -> None:
        self._first = first
        self._second = second

    def save(self, folder: str, filename: str, pdf: bytes) -> str:
        """The second archive is saved to before the first, so it keeps the PDF even when
        the first cannot be reached. That failure is still raised: the document is not
        reported as collected with a link that leads nowhere, and the next run files it."""
        self._second.save(folder, filename, pdf)
        return self._first.save(folder, filename, pdf)

    def remove(self, folder: str, filename: str, pdf: bytes) -> None:
        """Removes the copy from each. One that cannot be reached does not stop the other,
        and its failure is raised once both have been tried."""
        failures: list[Exception] = []
        for archive in (self._first, self._second):
            try:
                archive.remove(folder, filename, pdf)
            except Exception as failure:
                failures.append(failure)
        if failures:
            raise failures[0]


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

    def remove(self, folder: str, filename: str, pdf: bytes) -> None:
        """A folder left empty goes too."""
        directory = self._root / folder
        if not directory.is_dir():
            return
        for path in sorted(directory.iterdir()):
            if path.is_file() and is_named(path.name, filename) and path.read_bytes() == pdf:
                path.unlink()
                if not any(directory.iterdir()):
                    directory.rmdir()
                return

    @staticmethod
    def _free_path(directory: Path, name: Path, pdf: bytes) -> Path:
        candidate = directory / name
        number = 2
        while candidate.exists() and candidate.read_bytes() != pdf:
            candidate = directory / f"{name.stem}_{number}{name.suffix}"
            number += 1
        return candidate
