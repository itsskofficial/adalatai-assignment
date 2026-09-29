"""A golden set: every email in a sample folder with its correct answers and hard-case labels.

There are two golden sets. The standard set is the generated sample mail in `backend/samples`.
The hard set in `backend/evals/hard` holds the cases real invoices get wrong. Both have the
same layout, so the same eval runs on either.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from invoice_collector.domain import DocumentType, Email, EmailKind
from invoice_collector.samples import load_sources

BILLING_KINDS: frozenset[str] = frozenset({"invoice", "receipt", "credit_note"})
STANDARD = "standard"
HARD = "hard"
GOLDEN_SETS = (STANDARD, HARD)


@dataclass(frozen=True)
class GoldenCase:
    """One sample email and what the pipeline should make of it."""

    source_account: str
    file_name: str
    email: Email
    kind: EmailKind
    labels: tuple[str, ...] = ()
    invoice_format: str | None = None
    vendor: str | None = None
    invoice_date: date | None = None
    total: Decimal | None = None
    currency: str | None = None
    document_type: DocumentType | None = None
    portal_url: str | None = None
    attachment: str | None = None
    """The PDF attachment these answers are for, when the email carries more than one."""

    @property
    def email_key(self) -> str:
        """Names the email: its source account and file."""
        return f"{self.source_account}/{self.file_name}"

    @property
    def key(self) -> str:
        """Names the case in the scorecard: the email, and the attachment when one is named."""
        return self.email_key + (f"#{self.attachment}" if self.attachment else "")

    @property
    def is_billing_document(self) -> bool:
        return self.kind in BILLING_KINDS


def _all_emails(samples: Path) -> dict[tuple[str, str], Email]:
    ever = (datetime(1970, 1, 1, tzinfo=UTC), datetime(9999, 1, 1, tzinfo=UTC))
    return {
        (source.source_account, email.message_id): email
        for source in load_sources(samples)
        for email in source.emails_between(*ever)
    }


def _optional_date(value: Any) -> date | None:
    return None if value is None else date.fromisoformat(str(value))


def _optional_decimal(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


def load_golden(samples: Path) -> list[GoldenCase]:
    """The golden dataset beside the committed sample emails, in a fixed order."""
    entries = cast(list[dict[str, Any]], json.loads((samples / "golden.json").read_text("utf-8")))
    emails = _all_emails(samples)
    cases = [
        GoldenCase(
            source_account=str(entry["source_account"]),
            file_name=str(entry["file_name"]),
            email=emails[(str(entry["source_account"]), str(entry["message_id"]))],
            kind=cast(EmailKind, entry["kind"]),
            labels=tuple(sorted(str(label) for label in entry.get("labels") or ())),
            invoice_format=_optional_str(entry.get("invoice_format")),
            vendor=_optional_str(entry.get("vendor")),
            invoice_date=_optional_date(entry.get("invoice_date")),
            total=_optional_decimal(entry.get("total")),
            currency=_optional_str(entry.get("currency")),
            document_type=cast(DocumentType | None, entry.get("document_type")),
            portal_url=_optional_str(entry.get("portal_url")),
            attachment=_optional_str(entry.get("attachment")),
        )
        for entry in entries
    ]
    return sorted(cases, key=lambda case: case.key)


def one_per_email(cases: Sequence[GoldenCase]) -> list[GoldenCase]:
    """Each email once, for the jobs that judge an email and not a document in it."""
    seen: dict[str, GoldenCase] = {}
    for case in cases:
        seen.setdefault(case.email_key, replace(case, attachment=None))
    return list(seen.values())


def load_expected_vendors(samples: Path) -> tuple[str, ...]:
    entries = cast(
        list[dict[str, Any]], json.loads((samples / "expected_vendors.json").read_text("utf-8"))
    )
    return tuple(dict.fromkeys(str(entry["vendor"]) for entry in entries))
