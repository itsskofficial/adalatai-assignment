"""Reads the billing fields from a PDF."""

import hashlib
from collections.abc import Mapping
from typing import Protocol, Self

from invoice_collector.domain import Extraction


class ExtractionFailed(Exception):
    pass


class Extractor(Protocol):
    def extract(self, pdf: bytes) -> Extraction: ...


def content_hash(pdf: bytes) -> str:
    return hashlib.sha256(pdf).hexdigest()


class FakeExtractor:
    """Returns prepared answers, keyed by the content of the PDF."""

    def __init__(self, answers: Mapping[bytes, Extraction]) -> None:
        self._answers = {content_hash(pdf): answer for pdf, answer in answers.items()}

    @classmethod
    def from_hashes(cls, answers: Mapping[str, Extraction]) -> Self:
        extractor = cls({})
        extractor._answers = dict(answers)
        return extractor

    def extract(self, pdf: bytes) -> Extraction:
        try:
            return self._answers[content_hash(pdf)]
        except KeyError:
            raise ExtractionFailed("no prepared answer for this document") from None
