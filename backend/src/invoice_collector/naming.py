"""How the file of a billing document is named."""

import re

from invoice_collector.domain import Extraction


def filename(extraction: Extraction) -> str:
    vendor = re.sub(r"[^A-Za-z0-9]+", "", extraction.vendor)
    return (
        f"{extraction.invoice_date:%Y-%m}_{vendor}_{extraction.total:.2f}-{extraction.currency}.pdf"
    )
