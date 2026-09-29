"""The digest: the short message sent to Slack after a run.

It gives what was collected, the gaps and what needs review, and links to the summary
and to the dashboard's review screen. It is sent by a Slack incoming webhook.
"""

import json
import re
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal, Protocol
from urllib.parse import urlparse

from invoice_collector.domain import CollectionMonth, EmailState, ModelUsage
from invoice_collector.ledger import Ledger
from invoice_collector.metering import describe_cost
from invoice_collector.run import RunResult

GapState = Literal["missing", "unknown"]
Gap = tuple[str, GapState, str | None]
"""An expected vendor with no billing document: vendor, missing or unknown, explanation."""

EmailLine = tuple[str, str | None]
"""An email named in the digest: its subject and the recorded reason."""

# Slack's limits for a message.
MAX_BLOCKS = 50
MAX_TEXT = 3000
MAX_HEADER = 150

# How many entries a list shows before "and N more".
LISTED = 5
LISTED_GAPS = 15
LISTED_ACCOUNTS = 15

CUT_SHORT = "\n… cut short to fit Slack's limits; see the dashboard for the rest"


@dataclass(frozen=True)
class Digest:
    month: CollectionMonth
    documents_collected: int = 0
    rupee_total: Decimal = Decimal(0)
    charges_without_rupees: int = 0
    needs_review_items: tuple[EmailLine, ...] = ()
    skipped: int = 0
    failed_items: tuple[EmailLine, ...] = ()
    gaps: tuple[Gap, ...] | None = None
    """None when gaps were not checked; empty when there are none."""
    failed_source_accounts: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    summary_link: str | None = None
    review_link: str | None = None
    model_usage: tuple[ModelUsage, ...] | None = None
    """The run's calls to each model; None when they were not metered."""

    @property
    def needs_review(self) -> int:
        return len(self.needs_review_items)

    @property
    def failed(self) -> int:
        return len(self.failed_items)

    @property
    def needs_attention(self) -> bool:
        return bool(
            self.needs_review_items
            or self.failed_items
            or self.gaps
            or self.failed_source_accounts
            or self.warnings
        )


def build_digest(
    month: CollectionMonth,
    result: RunResult,
    ledger: Ledger,
    *,
    gaps: Sequence[Gap] | None = None,
    failed_source_accounts: Sequence[str] = (),
    summary_link: str | None = None,
    dashboard_url: str | None = None,
) -> Digest:
    examined = ledger.examined_emails(month)

    def lines(state: EmailState) -> tuple[EmailLine, ...]:
        return tuple((e.subject, e.reason) for e in examined if e.state == state)

    rupees = [row.inr_total for row in result.summary]
    return Digest(
        month=month,
        # The same billing document found in several source accounts is one document.
        documents_collected=len({d.content_hash for d in ledger.documents(month)}),
        rupee_total=sum((r for r in rupees if r is not None), Decimal(0)),
        charges_without_rupees=sum(1 for r in rupees if r is None),
        needs_review_items=lines(EmailState.NEEDS_REVIEW),
        skipped=sum(1 for e in examined if e.state == EmailState.SKIPPED),
        failed_items=lines(EmailState.FAILED),
        gaps=tuple(gaps) if gaps is not None else None,
        failed_source_accounts=tuple(failed_source_accounts),
        warnings=tuple(result.warnings),
        summary_link=summary_link,
        review_link=(
            f"{dashboard_url.rstrip('/')}/review?month={month}" if dashboard_url else None
        ),
        model_usage=result.model_usage,
    )


def escape(text: str) -> str:
    """Slack reads &, < and > as markup, so text from an email must escape them."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def rupees(amount: Decimal) -> str:
    """A rupee amount with Indian digit grouping, e.g. ₹1,23,456.00."""
    sign = "-" if amount < 0 else ""
    whole, fraction = f"{abs(amount):.2f}".split(".")
    groups = [whole[-3:]]
    rest = whole[:-3]
    while rest:
        groups.insert(0, rest[-2:])
        rest = rest[:-2]
    return f"{sign}₹{','.join(groups)}.{fraction}"


def _plural(count: int, one: str, many: str) -> str:
    return one if count == 1 else many


def _fit(text: str, limit: int = MAX_TEXT) -> str:
    """The text, cut short with a clear marker when it is over Slack's limit."""
    if len(text) <= limit:
        return text
    room = limit - len(CUT_SHORT)
    cut = text[:room]
    line_end = cut.rfind("\n")
    if line_end > room // 2:
        cut = cut[:line_end]
    else:
        # Never leave half of an escaped character such as &amp; at the end.
        ampersand = cut.rfind("&", max(0, len(cut) - 5))
        if ampersand != -1 and ";" not in cut[ampersand:]:
            cut = cut[:ampersand]
    return cut + CUT_SHORT


def _listed(entries: Sequence[str], limit: int) -> str:
    shown = [f"• {entry}" for entry in entries[:limit]]
    if len(entries) > limit:
        shown.append(f"… and {len(entries) - limit} more")
    return "\n".join(shown)


def _email_line(line: EmailLine) -> str:
    subject, reason = line
    return f"{escape(subject)}: {escape(reason)}" if reason else escape(subject)


def _gap_line(gap: Gap) -> str:
    vendor, state, explanation = gap
    line = f"{escape(vendor)}: {state}"
    return f"{line} ({escape(explanation)})" if explanation else line


def _gaps_figure(gaps: tuple[Gap, ...] | None) -> tuple[str, str]:
    if gaps is None:
        return "gaps not checked", "gaps not checked"
    if not gaps:
        return "no gaps", "no gaps"
    word = _plural(len(gaps), "gap", "gaps")
    return f"*{len(gaps)}* {word}", f"{len(gaps)} {word}"


def _headline(digest: Digest) -> tuple[str, str]:
    """The headline numbers, in Slack markup and in plain text."""
    spend = f"{rupees(digest.rupee_total)} spent"
    if digest.charges_without_rupees:
        count = digest.charges_without_rupees
        spend += f" ({count} {_plural(count, 'charge has', 'charges have')} no rupee amount)"
    documents = _plural(digest.documents_collected, "billing document", "billing documents")
    figures = [
        (str(digest.documents_collected), f"{documents} collected"),
        (None, spend),
        (str(digest.needs_review), "need review"),
        (str(digest.skipped), "skipped"),
        (str(digest.failed), "failed"),
    ]
    marked = [f"*{n}* {label}" if n else f"*{label}*" for n, label in figures]
    plain = [f"{n} {label}" if n else label for n, label in figures]
    gaps_marked, gaps_plain = _gaps_figure(digest.gaps)
    cost = f"model cost {escape(describe_cost(digest.model_usage))}"
    return " · ".join([*marked, gaps_marked, cost]), ", ".join([*plain, gaps_plain, cost])


def _section(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": _fit(text)}}


def _header(text: str) -> dict[str, Any]:
    return {"type": "header", "text": {"type": "plain_text", "text": _fit(text, MAX_HEADER)}}


def _is_web_link(link: str) -> bool:
    parsed = urlparse(link)
    return (
        parsed.scheme in ("http", "https")
        and bool(parsed.netloc)
        and not any(c in link for c in "<>| \n")
    )


def _link(link: str, label: str) -> str:
    return f"<{link}|{label}>" if _is_web_link(link) else f"{label}: `{escape(link)}`"


def _within_block_limit(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(blocks) <= MAX_BLOCKS:
        return blocks
    return [*blocks[: MAX_BLOCKS - 1], _section(CUT_SHORT.strip())]


def render(digest: Digest) -> dict[str, Any]:
    """The digest as a Slack message: Block Kit blocks with a plain text fallback."""
    marked, plain = _headline(digest)
    blocks = [_header(f"Digest for collection month {digest.month}"), _section(marked)]

    if not digest.needs_attention:
        quiet = "Nothing needs attention this month."
        if digest.gaps is None:
            quiet = "Nothing needs attention this month, but gaps were not checked."
        blocks.append(_section(quiet))
        plain += f". {quiet}"

    if digest.needs_review_items:
        entries = [_email_line(line) for line in digest.needs_review_items]
        blocks.append(
            _section(f"*Needs review ({digest.needs_review})*\n{_listed(entries, LISTED)}")
        )
    if digest.gaps:
        entries = [_gap_line(gap) for gap in digest.gaps]
        blocks.append(_section(f"*Gaps ({len(digest.gaps)})*\n{_listed(entries, LISTED_GAPS)}"))
    if digest.failed_source_accounts:
        accounts = digest.failed_source_accounts
        entries = [escape(account) for account in accounts]
        blocks.append(
            _section(
                f"*Source accounts that failed to sync ({len(accounts)})*\n"
                f"{_listed(entries, LISTED_ACCOUNTS)}"
            )
        )
    if digest.failed_items:
        entries = [_email_line(line) for line in digest.failed_items]
        blocks.append(_section(f"*Failed ({digest.failed})*\n{_listed(entries, LISTED)}"))
    if digest.warnings:
        entries = [escape(warning) for warning in digest.warnings]
        blocks.append(_section(f"*Warnings ({len(digest.warnings)})*\n{_listed(entries, LISTED)}"))

    links: list[str] = []
    if digest.summary_link:
        links.append(_link(digest.summary_link, "Summary"))
    if digest.review_link:
        links.append(_link(digest.review_link, "Review"))
    if links:
        blocks.append(_section(" · ".join(links)))

    return {
        "text": _fit(f"Digest for collection month {digest.month}: {plain}"),
        "blocks": _within_block_limit(blocks),
    }


def render_failure(month: CollectionMonth, error_message: str) -> dict[str, Any]:
    """The message sent when a run fails before it finishes."""
    reason = escape(error_message.strip() or "no reason was given")
    return {
        "text": _fit(f"The run for collection month {month} failed: {reason}"),
        "blocks": [
            _header(f"Run failed for collection month {month}"),
            _section(f"The run did not finish, so no digest was produced.\n*Reason:* {reason}"),
        ],
    }


class DigestNotSent(Exception):
    """The digest could not be sent. The reason never contains the webhook address."""


class DigestSender(Protocol):
    def send(self, message: dict[str, Any]) -> None:
        """Sends one message. Raises DigestNotSent."""
        ...


class FakeDigestSender:
    """Records the messages it is given instead of sending them."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def send(self, message: dict[str, Any]) -> None:
        self.sent.append(message)


SLACK_WEBHOOK_HOST = "hooks.slack.com"

# Slack answers a refused message with a short error code such as invalid_payload.
_SLACK_ERROR_CODE = re.compile(r"[a-z_]{1,64}")


class SlackWebhook:
    """Posts messages to a Slack incoming webhook.

    The webhook address is a secret: anyone holding it can post to the channel. It is
    never put in an error message or in this object's repr.
    """

    def __init__(self, url: str, *, allow_any_host: bool = False, timeout: float = 10) -> None:
        parsed = urlparse(url)
        is_slack = parsed.scheme == "https" and parsed.hostname == SLACK_WEBHOOK_HOST
        if not (is_slack or (allow_any_host and parsed.scheme in ("http", "https"))):
            raise ValueError(f"a Slack webhook must be an https://{SLACK_WEBHOOK_HOST}/ address")
        self._url = url
        self._timeout = timeout

    def __repr__(self) -> str:
        return "SlackWebhook(<secret>)"

    def send(self, message: dict[str, Any]) -> None:
        request = urllib.request.Request(
            self._url,
            data=json.dumps(message).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "invoice-collector"},
            method="POST",
        )
        # Raised outside the handlers, with no cause, so no traceback carries the address.
        reason: str | None = None
        try:
            with urllib.request.urlopen(request, timeout=self._timeout):
                pass
        except urllib.error.HTTPError as error:
            answer = error.read(64).decode("ascii", "replace").strip()
            reason = f"Slack refused the digest: HTTP {error.code}"
            if _SLACK_ERROR_CODE.fullmatch(answer):
                reason += f" ({answer})"
        except (urllib.error.URLError, TimeoutError, OSError):
            reason = "Slack could not be reached"
        if reason is not None:
            raise DigestNotSent(reason)
