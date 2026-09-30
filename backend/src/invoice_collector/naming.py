"""How the file of a billing document is named."""

import re
from datetime import date
from decimal import Decimal
from typing import Protocol


class NamedFields(Protocol):
    """The fields of a billing document its file is named from."""

    @property
    def vendor(self) -> str: ...

    @property
    def invoice_date(self) -> date: ...

    @property
    def total(self) -> Decimal: ...

    @property
    def currency(self) -> str: ...


def filename(extraction: NamedFields) -> str:
    vendor = re.sub(r"[^A-Za-z0-9]+", "", extraction.vendor)
    return (
        f"{extraction.invoice_date:%Y-%m}_{vendor}_{extraction.total:.2f}-{extraction.currency}.pdf"
    )
