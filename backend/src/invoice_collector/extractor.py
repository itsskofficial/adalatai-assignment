"""Reads the billing fields from a PDF."""

import hashlib
from collections.abc import Mapping
from typing import Protocol, Self

from invoice_collector.domain import Extraction


class ExtractionFailed(Exception):
    """The fields could not be read."""


class NotABillingDocument(Exception):
    """The document was read, and it is not an invoice, a receipt or a credit note."""


class Extractor(Protocol):
    def extract(self, pdf: bytes) -> Extraction:
        """Raises ExtractionFailed or NotABillingDocument."""
        ...


def content_hash(pdf: bytes) -> str:
    return hashlib.sha256(pdf).hexdigest()


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

    def extract(self, pdf: bytes) -> Extraction:
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

    def extract(self, pdf: bytes) -> Extraction:
        failures: list[str] = []
        for extractor in self._extractors:
            try:
                return extractor.extract(pdf)
            except ExtractionFailed as failure:
                failures.append(str(failure))
        raise ExtractionFailed("; then ".join(failures))
