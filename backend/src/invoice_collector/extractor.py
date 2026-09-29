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
    """Returns prepared answers, keyed by the content hash of the PDF."""

    def __init__(self, answers_by_hash: Mapping[str, Extraction]) -> None:
        self._answers = dict(answers_by_hash)

    @classmethod
    def for_documents(cls, answers: Mapping[bytes, Extraction]) -> Self:
        return cls({content_hash(pdf): answer for pdf, answer in answers.items()})

    def extract(self, pdf: bytes) -> Extraction:
        try:
            return self._answers[content_hash(pdf)]
        except KeyError:
            raise ExtractionFailed("no prepared answer for this document") from None
