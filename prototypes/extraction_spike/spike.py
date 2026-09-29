# /// script
# requires-python = ">=3.12"
# dependencies = ["anthropic", "pydantic", "playwright", "python-dotenv"]
# ///
"""PROTOTYPE, throwaway. Answers one question:

    Can one email of each invoice format be turned into a correctly named PDF
    with correctly extracted fields?

Run: uv run prototypes/extraction_spike/spike.py
Needs ANTHROPIC_API_KEY in the environment or in a .env file at the repo root.
"""

import base64
import json
import re
import threading
import time
from decimal import Decimal
from email import message_from_bytes, policy
from email.message import EmailMessage
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Literal

import anthropic
from dotenv import load_dotenv
from playwright.sync_api import Browser, sync_playwright
from pydantic import BaseModel, Field

HERE = Path(__file__).parent
SAMPLES = HERE / "samples"
PORTAL = HERE / "portal"
OUT = HERE / "out"
MODEL = "claude-haiku-4-5"

EXPECTED = {
    "01_attachment_slack.eml": ("Slack", "2026-08-03", "652.50", "USD", "invoice"),
    "02_body_notion.eml": ("Notion", "2026-08-14", "221.40", "EUR", "receipt"),
    "03_portal_figma.eml": ("Figma", "2026-08-21", "190.00", "USD", "invoice"),
}


class Extraction(BaseModel):
    document_type: Literal["invoice", "receipt", "credit_note", "not_a_billing_document"]
    vendor: str = Field(description="Short brand name of the vendor, e.g. 'Slack', not the legal entity")
    invoice_date: str = Field(description="Date printed on the document, as YYYY-MM-DD")
    total: str = Field(description="Total including tax, digits and decimal point only, e.g. '1250.00'")
    currency: str = Field(description="ISO 4217 code, e.g. 'USD'")
    confidence: Literal["high", "medium", "low"]
    doubts: str = Field(description="Anything ambiguous about these fields, or an empty string")


PROMPT = """Extract the billing fields from this document, which a SaaS vendor sent to a company.

The total is the final amount including tax. The invoice date is the date printed on the \
document, not a billing period and not a due date. Report low confidence if any field had to be guessed."""


def html_part(msg: EmailMessage) -> str | None:
    part = msg.get_body(preferencelist=("html",))
    return part.get_content() if part else None


def pdf_attachment(msg: EmailMessage) -> bytes | None:
    for part in msg.iter_attachments():
        if part.get_content_type() == "application/pdf":
            return part.get_content()
    return None


def classify_format(msg: EmailMessage) -> tuple[str, bytes | str]:
    """Rule-based routing. Returns the format and the thing to work on."""
    pdf = pdf_attachment(msg)
    if pdf:
        return "attachment", pdf
    html = html_part(msg) or ""
    has_amount = re.search(r"[$€£₹]\s?\d|\d\s?(USD|EUR|GBP|INR)", html)
    links = re.findall(r'href="(https?://[^"]+)"', html)
    if links and not has_amount:
        return "portal_link", links[0]
    return "body", html


def render_pdf(browser: Browser, *, html: str | None = None, url: str | None = None) -> bytes:
    page = browser.new_page()
    if url:
        page.goto(url, wait_until="networkidle")
    else:
        page.set_content(html or "")
    pdf = page.pdf(format="A4", print_background=True)
    page.close()
    return pdf


def extract(client: anthropic.Anthropic, pdf: bytes) -> tuple[Extraction, dict]:
    started = time.perf_counter()
    response = client.messages.parse(
        model=MODEL,
        max_tokens=1024,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": base64.standard_b64encode(pdf).decode(),
                        },
                    },
                    {"type": "text", "text": PROMPT},
                ],
            }
        ],
        output_format=Extraction,
    )
    usage = {
        "seconds": round(time.perf_counter() - started, 2),
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
    }
    return response.parsed_output, usage


def filename(e: Extraction) -> str:
    vendor = re.sub(r"[^A-Za-z0-9]+", "", e.vendor)
    total = f"{Decimal(e.total):.2f}"
    return f"{e.invoice_date[:7]}_{vendor}_{total}-{e.currency}.pdf"


def main() -> None:
    load_dotenv(HERE.parents[1] / ".env")
    client = anthropic.Anthropic()
    OUT.mkdir(exist_ok=True)

    handler = partial(SimpleHTTPRequestHandler, directory=str(PORTAL))
    server = ThreadingHTTPServer(("localhost", 8765), handler)
    server.RequestHandlerClass.log_message = lambda *a, **k: None
    threading.Thread(target=server.serve_forever, daemon=True).start()

    passed = 0
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for path in sorted(SAMPLES.glob("*.eml")):
            msg = message_from_bytes(path.read_bytes(), policy=policy.default)
            fmt, payload = classify_format(msg)

            if fmt == "attachment":
                pdf = payload
            elif fmt == "body":
                pdf = render_pdf(browser, html=payload)
            else:
                pdf = render_pdf(browser, url=payload)

            extraction, usage = extract(client, pdf)
            name = filename(extraction)
            (OUT / name).write_bytes(pdf)

            got = (
                extraction.vendor,
                extraction.invoice_date,
                f"{Decimal(extraction.total):.2f}",
                extraction.currency,
                extraction.document_type,
            )
            ok = got == EXPECTED[path.name]
            passed += ok

            print("=" * 72)
            print(f"{path.name}   from: {msg['From']}")
            print(f"  format   : {fmt}")
            print(f"  extracted: {json.dumps(extraction.model_dump(), ensure_ascii=False)}")
            print(f"  expected : {EXPECTED[path.name]}")
            print(f"  result   : {'PASS' if ok else 'FAIL'}")
            print(f"  saved as : out/{name}  ({len(pdf)} bytes)")
            print(f"  cost     : {usage}")
        browser.close()

    server.shutdown()
    print("=" * 72)
    print(f"{passed}/{len(EXPECTED)} samples extracted correctly with {MODEL}")


if __name__ == "__main__":
    main()
