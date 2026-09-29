# /// script
# requires-python = ">=3.12"
# dependencies = ["playwright"]
# ///
"""PROTOTYPE, throwaway. Builds one sample email per invoice format into samples/.

Run: uv run prototypes/extraction_spike/make_samples.py
"""

from email.message import EmailMessage
from email.utils import format_datetime
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).parent
SAMPLES = HERE / "samples"
PORTAL = HERE / "portal"

STYLE = """
<style>
  body { font-family: Helvetica, Arial, sans-serif; color: #1d1c1d; margin: 40px; }
  h1 { font-size: 22px; margin-bottom: 4px; }
  .muted { color: #616061; font-size: 13px; }
  table { border-collapse: collapse; width: 100%; margin-top: 24px; }
  th, td { text-align: left; padding: 8px 6px; border-bottom: 1px solid #ddd; font-size: 14px; }
  td.num, th.num { text-align: right; }
  .total td { font-weight: bold; border-top: 2px solid #1d1c1d; }
</style>
"""

SLACK_INVOICE_HTML = f"""<html><head>{STYLE}</head><body>
<h1>Slack Technologies Limited</h1>
<div class="muted">Salesforce Tower, 60 R801, North Dock, Dublin, Ireland</div>
<h2>Invoice SBIE-8841207</h2>
<p>Invoice date: August 3, 2026<br>Billing period: Aug 3, 2026 to Sep 2, 2026<br>
Bill to: Nyaya Labs Pvt Ltd, Bengaluru, India</p>
<table>
  <tr><th>Description</th><th class="num">Qty</th><th class="num">Unit price</th><th class="num">Amount</th></tr>
  <tr><td>Business+ plan, monthly</td><td class="num">42</td><td class="num">$15.00</td><td class="num">$630.00</td></tr>
  <tr><td>Prorated seats added in July</td><td class="num">3</td><td class="num">$7.50</td><td class="num">$22.50</td></tr>
  <tr><td>Subtotal</td><td></td><td></td><td class="num">$652.50</td></tr>
  <tr><td>Tax (0%)</td><td></td><td></td><td class="num">$0.00</td></tr>
  <tr class="total"><td>Total due</td><td></td><td></td><td class="num">$652.50 USD</td></tr>
</table>
</body></html>"""

NOTION_BODY_HTML = f"""<html><head>{STYLE}</head><body>
<p>Hi Nyaya Labs,</p>
<p>Thanks for using Notion. This email is your receipt for your latest payment.</p>
<h2>Receipt #2391-7745</h2>
<p class="muted">Paid on 14 Aug 2026 with Visa ending 4242</p>
<table>
  <tr><th>Item</th><th class="num">Amount</th></tr>
  <tr><td>Notion Plus plan, 18 members, 14 Aug 2026 to 14 Sep 2026</td><td class="num">€180.00</td></tr>
  <tr><td>VAT (23%)</td><td class="num">€41.40</td></tr>
  <tr class="total"><td>Total paid</td><td class="num">€221.40</td></tr>
</table>
<p class="muted">Notion Labs, Inc. 2300 Harrison Street, San Francisco, CA 94110</p>
</body></html>"""

FIGMA_PORTAL_HTML = f"""<html><head>{STYLE}</head><body>
<h1>Figma, Inc.</h1>
<div class="muted">760 Market Street, Floor 10, San Francisco, CA 94102</div>
<h2>Invoice FIG-2026-08-55102</h2>
<p>Date of issue: 2026-08-21<br>Bill to: Nyaya Labs Pvt Ltd</p>
<table>
  <tr><th>Description</th><th class="num">Qty</th><th class="num">Amount</th></tr>
  <tr><td>Professional team, Full seats, monthly</td><td class="num">6</td><td class="num">$90.00</td></tr>
  <tr><td>Dev Mode seats, monthly</td><td class="num">4</td><td class="num">$100.00</td></tr>
  <tr class="total"><td>Amount due</td><td></td><td class="num">US$190.00</td></tr>
</table>
</body></html>"""

PORTAL_TOKEN = "in_1PqX7fK2eZvKYlo2Cq9m8dTa"


def base_message(sender: str, subject: str, when: datetime) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = "finance@nyayalabs.example"
    msg["Subject"] = subject
    msg["Date"] = format_datetime(when)
    return msg


def main() -> None:
    SAMPLES.mkdir(exist_ok=True)
    PORTAL.mkdir(exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(SLACK_INVOICE_HTML)
        slack_pdf = page.pdf(format="A4")
        browser.close()

    attachment = base_message(
        "Slack <feedback@slack.com>",
        "Your Slack invoice is available",
        datetime(2026, 8, 3, 9, 12, tzinfo=timezone.utc),
    )
    attachment.set_content("Your invoice for Nyaya Labs is attached.\n\nThe Slack team")
    attachment.add_attachment(
        slack_pdf, maintype="application", subtype="pdf", filename="Invoice-SBIE-8841207.pdf"
    )
    (SAMPLES / "01_attachment_slack.eml").write_bytes(bytes(attachment))

    body = base_message(
        "Notion <team@mail.notion.so>",
        "Your receipt from Notion #2391-7745",
        datetime(2026, 8, 14, 17, 40, tzinfo=timezone.utc),
    )
    body.set_content("Receipt #2391-7745. Total paid EUR 221.40. View this email in HTML.")
    body.add_alternative(NOTION_BODY_HTML, subtype="html")
    (SAMPLES / "02_body_notion.eml").write_bytes(bytes(body))

    (PORTAL / f"{PORTAL_TOKEN}.html").write_text(FIGMA_PORTAL_HTML, encoding="utf-8")
    link = base_message(
        "Figma <billing@figma.com>",
        "Your Figma invoice is ready",
        datetime(2026, 8, 21, 6, 5, tzinfo=timezone.utc),
    )
    url = f"http://localhost:8765/{PORTAL_TOKEN}.html"
    link.set_content(f"Your latest invoice is ready. View it here: {url}")
    link.add_alternative(
        f'<html><body><p>Your latest invoice is ready.</p><p><a href="{url}">View invoice</a></p></body></html>',
        subtype="html",
    )
    (SAMPLES / "03_portal_figma.eml").write_bytes(bytes(link))

    for f in sorted(SAMPLES.iterdir()):
        print(f"wrote {f.relative_to(HERE)}  ({f.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
