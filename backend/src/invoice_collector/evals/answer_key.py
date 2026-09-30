"""The answer key generated with the sample mail: the right reading of each of its PDFs.

It is the golden data of extraction, in answers.json beside the sample emails, keyed by the
SHA-256 of each PDF. The eval and the tests read it. The collect command cannot: without a
model, rules read a document, and what they read is held for a person to confirm.
"""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from invoice_collector.domain import Extraction
from invoice_collector.extractor import FakeExtractor

ANSWERS_FILE = "answers.json"


def answer_key(samples: Path) -> FakeExtractor:
    """Reads each PDF of the sample folder as its answer key says, and fails on any other."""
    answers = json.loads((samples / ANSWERS_FILE).read_text(encoding="utf-8"))
    return FakeExtractor(
        {
            digest: Extraction(
                document_type=a["document_type"],
                vendor=a["vendor"],
                invoice_date=date.fromisoformat(a["invoice_date"]),
                total=Decimal(a["total"]),
                currency=a["currency"],
            )
            for digest, a in answers.items()
        }
    )
