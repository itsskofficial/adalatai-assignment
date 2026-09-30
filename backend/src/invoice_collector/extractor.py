"""Reads the billing fields from a PDF."""

import hashlib
import io
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol, Self

from pypdf import PdfReader
from pypdf.errors import FileNotDecryptedError, PyPdfError

from invoice_collector.domain import ExpectedVendor, Extraction


class ExtractionFailed(Exception):
    """The fields could not be read."""


class NotABillingDocument(Exception):
    """The document was read, and it is not an invoice, a receipt or a credit note."""


@dataclass(frozen=True)
class Hints:
    """What is known of a document before it is read: the names it may be from.

    A reader without a model needs them to tell who issued the document, since no list of
    vendors is kept in the code. They come from the expected vendor list in the ledger and
    from the email the document came in, its sender and what the classifier took it to be
    from. A model reads the document alone and is given none of them.
    """

    expected_vendors: tuple[str, ...] = ()
    named_by_email: tuple[str, ...] = ()


NO_HINTS = Hints()


def hints_for(expected_vendors: Iterable[ExpectedVendor], *named_by_email: str | None) -> Hints:
    """The names on the expected vendor list, and those the email gives, each once."""
    listed = dict.fromkeys(v.vendor.strip() for v in expected_vendors if v.vendor.strip())
    named = dict.fromkeys(n.strip() for n in named_by_email if n and n.strip())
    return Hints(tuple(listed), tuple(named))


class Extractor(Protocol):
    def extract(self, pdf: bytes, hints: Hints = NO_HINTS) -> Extraction:
        """Raises ExtractionFailed or NotABillingDocument."""
        ...


def content_hash(pdf: bytes) -> str:
    return hashlib.sha256(pdf).hexdigest()


def pdf_problem(pdf: bytes) -> str | None:
    """Why the PDF cannot be opened at all, or None when it opens.

    A PDF that opens may still hold nothing readable, such as a scan; that is not a
    problem with the file.
    """
    try:
        reader = PdfReader(io.BytesIO(pdf))
        if reader.is_encrypted:
            try:
                # Many PDFs are encrypted with an empty password and open for anyone.
                if not reader.decrypt(""):
                    return "the PDF is password-protected"
            except PyPdfError:
                return "the PDF is password-protected"
        if not reader.pages:
            return "the PDF is damaged"
    except FileNotDecryptedError:
        return "the PDF is password-protected"
    except (PyPdfError, ValueError, OSError):
        return "the PDF is damaged"
    return None


def pdf_text(pdf: bytes) -> tuple[str, int]:
    """The text of a PDF and its page count; empty and zero when it cannot be read."""
    try:
        reader = PdfReader(io.BytesIO(pdf))
        return "\n".join(page.extract_text() for page in reader.pages), len(reader.pages)
    except (PyPdfError, ValueError, OSError):
        return "", 0


class FakeExtractor:
    """Returns prepared answers, keyed by the content hash of the PDF."""

    def __init__(
        self,
        answers_by_hash: Mapping[str, Extraction],
        not_billing: frozenset[str] = frozenset(),
    ) -> None:
        self._answers = dict(answers_by_hash)
        self._not_billing = not_billing

    @classmethod
    def for_documents(
        cls, answers: Mapping[bytes, Extraction], not_billing: tuple[bytes, ...] = ()
    ) -> Self:
        return cls(
            {content_hash(pdf): answer for pdf, answer in answers.items()},
            frozenset(content_hash(pdf) for pdf in not_billing),
        )

    def extract(self, pdf: bytes, hints: Hints = NO_HINTS) -> Extraction:
        digest = content_hash(pdf)
        if digest in self._not_billing:
            raise NotABillingDocument("not a billing document")
        try:
            return self._answers[digest]
        except KeyError:
            raise ExtractionFailed("no prepared answer for this document") from None


class FallbackExtractor:
    """Tries each extractor in turn until one reads the document."""

    def __init__(self, *extractors: Extractor) -> None:
        self._extractors = extractors

    def extract(self, pdf: bytes, hints: Hints = NO_HINTS) -> Extraction:
        failures: list[str] = []
        for extractor in self._extractors:
            try:
                return extractor.extract(pdf, hints)
            except ExtractionFailed as failure:
                failures.append(str(failure))
        raise ExtractionFailed("; then ".join(failures))
