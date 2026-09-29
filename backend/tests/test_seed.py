"""The seed generator writes sample emails together with their correct answers."""

import base64
import csv
import hashlib
import json
import re
from collections import Counter
from datetime import date
from decimal import Decimal
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import pytest

from invoice_collector import cli as collector_cli
from invoice_collector.samples import load_sources
from invoice_collector.seed import SeedConfig, generate, load_messages, write_folder
from invoice_collector.seed.cli import main as seed_main
from invoice_collector.seed.gmail_insert import insert_messages

ACCOUNTS = (
    "engineering@nyayalabs.example",
    "ops@nyayalabs.example",
    "finance@nyayalabs.example",
)
HARD_CASES = {
    "newsletter",
    "promotion_with_price",
    "product_update",
    "security_alert",
    "duplicate_across_accounts",
    "invoice_and_receipt",
    "foreign_currency",
    "credit_note",
    "multi_page",
    "payment_failed",
    "renewal_reminder",
    "late_arrival",
    "anomaly",
    "gap",
    "tokenised_portal_link",
    "login_gated_portal_link",
}
BILLING_DOCUMENTS = {"invoice", "receipt", "credit_note"}

Golden = list[dict[str, Any]]


class FakeRenderer:
    """Stands in for the browser: a small, repeatable PDF for each HTML document."""

    def render_html(self, html: str) -> bytes:
        return b"%PDF-1.7 fake " + hashlib.sha256(html.encode()).hexdigest().encode()


def write_seed(folder: Path, config: SeedConfig | None = None) -> Path:
    write_folder(generate(config or SeedConfig(), FakeRenderer()), folder)
    return folder


@pytest.fixture(scope="module")
def folder(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return write_seed(tmp_path_factory.mktemp("seed"))


@pytest.fixture(scope="module")
def golden(folder: Path) -> Golden:
    return json.loads((folder / "golden.json").read_text(encoding="utf-8"))


def read_eml(path: Path) -> EmailMessage:
    message = message_from_bytes(path.read_bytes(), policy=policy.default)
    assert isinstance(message, EmailMessage)
    return message


def text_of(message: EmailMessage) -> tuple[list[tuple[str, str]], str, str]:
    plain = message.get_body(preferencelist=("plain",))
    html = message.get_body(preferencelist=("html",))
    return (
        [(name, str(value)) for name, value in message.items()],
        str(plain.get_content()) if plain else "",
        str(html.get_content()) if html else "",
    )


def eml_of(folder: Path, entry: dict[str, Any]) -> EmailMessage:
    return read_eml(folder / entry["source_account"] / entry["file_name"])


def labelled(golden: Golden, label: str, month: str = "2026-08") -> Golden:
    return [e for e in golden if label in e["labels"] and e["month"] == month]


def test_two_runs_give_the_same_golden_dataset_and_email_text(folder: Path, tmp_path: Path) -> None:
    again = write_seed(tmp_path / "again")

    assert (again / "golden.json").read_text(encoding="utf-8") == (
        folder / "golden.json"
    ).read_text(encoding="utf-8")
    first = sorted(p.relative_to(folder) for p in folder.rglob("*.eml"))
    second = sorted(p.relative_to(again) for p in again.rglob("*.eml"))
    assert first == second
    for path in first:
        assert text_of(read_eml(folder / path)) == text_of(read_eml(again / path))


def test_the_committed_samples_are_what_the_generator_writes(folder: Path) -> None:
    committed = Path(__file__).parent.parent / "samples"

    for name in ("golden.json", "expected_vendors.json"):
        assert (committed / name).read_text(encoding="utf-8") == (folder / name).read_text(
            encoding="utf-8"
        ), f"{name} is stale: run invoice-collector-seed generate"
    emails = sorted(p.relative_to(folder) for p in folder.rglob("*.eml"))
    assert sorted(p.relative_to(committed) for p in committed.rglob("*.eml")) == emails
    for path in emails:
        assert text_of(read_eml(committed / path)) == text_of(read_eml(folder / path))


def test_every_email_has_exactly_one_golden_entry_and_the_reverse(
    folder: Path, golden: Golden
) -> None:
    on_disk = sorted(
        (p.parent.name, p.name) for p in folder.rglob("*.eml") if p.parent.parent == folder
    )
    in_golden = sorted((e["source_account"], e["file_name"]) for e in golden)

    assert in_golden == on_disk
    assert len(set(in_golden)) == len(in_golden)


def test_golden_entries_carry_the_message_id_of_their_email(folder: Path, golden: Golden) -> None:
    for entry in golden:
        assert str(eml_of(folder, entry)["Message-ID"]).strip("<>") == entry["message_id"]
        assert entry["source_account"] in str(eml_of(folder, entry)["To"]) + str(
            eml_of(folder, entry)["Cc"]
        )


@pytest.mark.parametrize("label", sorted(HARD_CASES))
def test_each_hard_case_appears_in_the_target_month(golden: Golden, label: str) -> None:
    assert labelled(golden, label)


def test_each_source_account_has_about_fifteen_target_month_emails(golden: Golden) -> None:
    counts = Counter(e["source_account"] for e in golden if e["month"] == "2026-08")

    assert set(counts) == set(ACCOUNTS)
    for account in ACCOUNTS:
        assert abs(counts[account] - 15) <= 2


def test_history_months_are_lighter(golden: Golden) -> None:
    for month in ("2026-06", "2026-07"):
        counts = Counter(e["source_account"] for e in golden if e["month"] == month)
        for account in ACCOUNTS:
            assert abs(counts[account] - 8) <= 2


def test_every_email_is_readable_as_a_sample(folder: Path, golden: Golden) -> None:
    sources = {s.source_account: s for s in load_sources(folder)}
    start, end = (
        parsedate_to_datetime("Mon, 1 Jan 2024 00:00:00 +0000"),
        parsedate_to_datetime("Fri, 1 Jan 2038 00:00:00 +0000"),
    )

    read = sorted(
        (account, email.message_id)
        for account in ACCOUNTS
        for email in sources[account].emails_between(start, end)
    )

    assert read == sorted((e["source_account"], e["message_id"]) for e in golden)


def test_the_portal_pages_are_not_read_as_a_source_account(folder: Path) -> None:
    assert (folder / "portal").is_dir()

    assert sorted(s.source_account for s in load_sources(folder)) == sorted(ACCOUNTS)


def test_emails_have_the_headers_and_parts_of_real_mail(folder: Path, golden: Golden) -> None:
    for entry in golden:
        message = eml_of(folder, entry)
        for header in ("From", "To", "Subject", "Date", "Message-ID"):
            assert message[header], f"{entry['file_name']} has no {header}"
        assert message.get_body(preferencelist=("plain",)) is not None
        assert message.get_body(preferencelist=("html",)) is not None
        pdfs = [a for a in message.iter_attachments() if a.get_content_type() == "application/pdf"]
        assert bool(pdfs) == (entry["invoice_format"] == "attachment")


def test_totals_equal_the_line_items_plus_tax(golden: Golden) -> None:
    documents = [e for e in golden if e["kind"] in BILLING_DOCUMENTS]

    assert documents
    for entry in documents:
        lines = sum((Decimal(item["amount"]) for item in entry["line_items"]), Decimal(0))
        assert lines == Decimal(entry["subtotal"])
        assert lines + Decimal(entry["tax"]) == Decimal(entry["total"])


def test_billing_documents_are_fully_labelled_and_other_emails_are_not(golden: Golden) -> None:
    for entry in golden:
        if entry["kind"] in BILLING_DOCUMENTS:
            assert entry["document_type"] == entry["kind"]
            assert entry["invoice_format"] in {"attachment", "body", "portal_link"}
            assert entry["vendor"] and entry["currency"]
            date.fromisoformat(entry["invoice_date"])
        else:
            assert entry["kind"] in {"payment_failed", "renewal_reminder", "not_billing"}
            assert entry["invoice_format"] is None
            assert entry["total"] is None and entry["document_type"] is None


def test_all_three_invoice_formats_are_covered(golden: Golden) -> None:
    formats = {e["invoice_format"] for e in golden if e["month"] == "2026-08"}

    assert {"attachment", "body", "portal_link"} <= formats


def test_a_credit_note_has_a_negative_total(golden: Golden) -> None:
    for entry in labelled(golden, "credit_note"):
        assert entry["kind"] == "credit_note"
        assert Decimal(entry["total"]) < 0


def test_the_answers_file_covers_every_pdf_attachment(folder: Path, golden: Golden) -> None:
    answers = json.loads((folder / "answers.json").read_text(encoding="utf-8"))
    seen: set[str] = set()

    for entry in golden:
        for part in eml_of(folder, entry).iter_attachments():
            content = part.get_payload(decode=True)
            assert isinstance(content, bytes)
            digest = hashlib.sha256(content).hexdigest()
            seen.add(digest)
            assert answers[digest] == {
                "document_type": entry["document_type"],
                "vendor": entry["vendor"],
                "invoice_date": entry["invoice_date"],
                "total": entry["total"],
                "currency": entry["currency"],
            }

    assert seen == set(answers)


def test_the_collect_command_still_works_on_the_folder(folder: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"

    exit_code = collector_cli.main(
        ["collect", "2026-08", "--samples", str(folder), "--out", str(out)]
        + ["--extractor", "prepared", "--classifier", "rules", "--no-exchange-rates"]
    )

    assert exit_code == 0
    with (out / "2026-08_summary.csv").open(newline="", encoding="utf-8") as f:
        vendors = {row["vendor"] for row in csv.DictReader(f)}
    assert {"Slack", "AWS", "Atlassian"} <= vendors


def test_the_same_billing_document_arrives_in_two_source_accounts(
    folder: Path, golden: Golden
) -> None:
    by_charge: dict[str, list[dict[str, Any]]] = {}
    for entry in labelled(golden, "duplicate_across_accounts"):
        by_charge.setdefault(entry["charge"], []).append(entry)

    assert by_charge
    for copies in by_charge.values():
        assert len({e["source_account"] for e in copies}) == 2
        pdfs = {
            part.get_payload(decode=True)
            for e in copies
            for part in eml_of(folder, e).iter_attachments()
        }
        assert len(pdfs) == 1


def test_an_invoice_and_a_receipt_share_one_charge(golden: Golden) -> None:
    by_charge: dict[str, list[dict[str, Any]]] = {}
    for entry in labelled(golden, "invoice_and_receipt"):
        by_charge.setdefault(entry["charge"], []).append(entry)

    assert by_charge
    for pair in by_charge.values():
        assert sorted(e["kind"] for e in pair) == ["invoice", "receipt"]
        assert len({(e["total"], e["currency"], e["vendor"]) for e in pair}) == 1
        assert len({e["message_id"] for e in pair}) == 2


def test_a_late_arrival_is_dated_in_the_month_and_emailed_just_after(
    folder: Path, golden: Golden
) -> None:
    for entry in labelled(golden, "late_arrival"):
        emailed = parsedate_to_datetime(str(eml_of(folder, entry)["Date"]))
        assert entry["invoice_date"].startswith("2026-08")
        assert (emailed.year, emailed.month) == (2026, 9)
        assert emailed.day in (1, 2)


def test_the_expected_vendor_list_describes_each_vendor(folder: Path) -> None:
    expected = json.loads((folder / "expected_vendors.json").read_text(encoding="utf-8"))

    assert len(expected) >= 14
    assert {v["currency"] for v in expected} >= {"USD", "EUR", "GBP", "INR"}
    for vendor in expected:
        assert vendor["vendor"]
        assert vendor["source_account"] in ACCOUNTS
        assert Decimal(vendor["usual_amount"]) > 0
        if vendor["billing_cycle"] == "annual":
            assert 1 <= vendor["renewal_month"] <= 12
        else:
            assert vendor["billing_cycle"] == "monthly"
            assert vendor["renewal_month"] is None


def test_an_anomaly_is_about_forty_percent_above_the_usual_amount(
    folder: Path, golden: Golden
) -> None:
    expected = json.loads((folder / "expected_vendors.json").read_text(encoding="utf-8"))
    usual = {v["vendor"]: Decimal(v["usual_amount"]) for v in expected}

    for entry in labelled(golden, "anomaly"):
        ratio = Decimal(entry["total"]) / usual[entry["vendor"]]
        assert Decimal("1.35") <= ratio <= Decimal("1.45")

    ordinary = [
        e
        for e in golden
        if e["kind"] in {"invoice", "receipt"}
        and "anomaly" not in e["labels"]
        and e["vendor"] in usual
    ]
    for entry in ordinary:
        ratio = Decimal(entry["total"]) / usual[entry["vendor"]]
        assert Decimal("0.8") <= ratio <= Decimal("1.2"), entry["file_name"]


def test_one_expected_vendor_is_absent_in_the_target_month(folder: Path, golden: Golden) -> None:
    expected = json.loads((folder / "expected_vendors.json").read_text(encoding="utf-8"))
    monthly = {v["vendor"] for v in expected if v["billing_cycle"] == "monthly"}

    def billed(month: str) -> set[str]:
        return {
            e["vendor"]
            for e in golden
            if e["kind"] in BILLING_DOCUMENTS and e["invoice_date"].startswith(month)
        }

    absent = monthly - billed("2026-08")

    assert len(absent) == 1
    assert absent == {e["vendor"] for e in labelled(golden, "gap")}
    assert monthly <= billed("2026-06") and monthly <= billed("2026-07")


def test_one_vendor_in_history_is_not_an_expected_vendor(folder: Path, golden: Golden) -> None:
    expected = json.loads((folder / "expected_vendors.json").read_text(encoding="utf-8"))
    history = [e for e in golden if "suggested_vendor" in e["labels"]]

    assert {e["month"] for e in history} == {"2026-06", "2026-07"}
    assert len({e["vendor"] for e in history}) == 1
    assert history[0]["vendor"] not in {v["vendor"] for v in expected}


def test_a_renewal_reminder_is_for_an_annual_vendor(folder: Path, golden: Golden) -> None:
    expected = json.loads((folder / "expected_vendors.json").read_text(encoding="utf-8"))
    annual = {v["vendor"] for v in expected if v["billing_cycle"] == "annual"}

    for entry in labelled(golden, "renewal_reminder"):
        assert entry["kind"] == "renewal_reminder"
        assert entry["vendor"] in annual


def test_a_tokenised_portal_link_opens_the_billing_document(folder: Path, golden: Golden) -> None:
    for entry in labelled(golden, "tokenised_portal_link"):
        url: str = entry["portal_url"]
        assert url.startswith("http://localhost:8765/")
        assert url in text_of(eml_of(folder, entry))[1]
        assert f'href="{url}"' in text_of(eml_of(folder, entry))[2]
        page = (folder / "portal" / url.removeprefix("http://localhost:8765/")).read_text(
            encoding="utf-8"
        )
        assert 'type="password"' not in page
        assert entry["vendor"] in page


def test_a_login_gated_portal_link_leads_to_a_sign_in_page(folder: Path, golden: Golden) -> None:
    for entry in labelled(golden, "login_gated_portal_link"):
        url: str = entry["portal_url"]
        assert url in text_of(eml_of(folder, entry))[1]
        page = (folder / "portal" / url.removeprefix("http://localhost:8765/")).read_text(
            encoding="utf-8"
        )
        assert re.search(r'<input[^>]*type="password"', page)
        assert entry["total"] not in page.replace(",", "")


def test_portal_links_use_the_configured_base_url(tmp_path: Path) -> None:
    config = SeedConfig(portal_base_url="https://portal.nyayalabs.example/seed/")
    folder = write_seed(tmp_path / "seed", config)
    golden: Golden = json.loads((folder / "golden.json").read_text(encoding="utf-8"))

    links = [e["portal_url"] for e in golden if e["invoice_format"] == "portal_link"]

    assert links
    for url in links:
        assert url.startswith("https://portal.nyayalabs.example/seed/")
        assert "seed//" not in url
        assert (folder / "portal" / url.rsplit("/", 1)[1]).is_file()


def test_source_accounts_are_parameters(tmp_path: Path) -> None:
    accounts = ("dev@acme.example", "office@acme.example", "accounts@acme.example")
    folder = write_seed(tmp_path / "seed", SeedConfig(source_accounts=accounts))
    golden: Golden = json.loads((folder / "golden.json").read_text(encoding="utf-8"))
    expected = json.loads((folder / "expected_vendors.json").read_text(encoding="utf-8"))

    assert {e["source_account"] for e in golden} == set(accounts)
    assert {v["source_account"] for v in expected} == set(accounts)
    assert all((folder / account).is_dir() for account in accounts)


def test_writing_again_replaces_earlier_emails(tmp_path: Path) -> None:
    folder = tmp_path / "seed"
    (folder / ACCOUNTS[0]).mkdir(parents=True)
    (folder / ACCOUNTS[0] / "hand-made.eml").write_bytes(b"Subject: old\n\nold")

    write_seed(folder)

    assert not (folder / ACCOUNTS[0] / "hand-made.eml").exists()


class FakeRequest:
    def __init__(self, result: dict[str, Any]) -> None:
        self._result = result

    def execute(self) -> dict[str, Any]:
        return self._result


class FakeGmail:
    """A mailbox behind the few Gmail API calls the insert makes."""

    def __init__(self, present: set[str]) -> None:
        self.present = set(present)
        self.queries: list[dict[str, Any]] = []
        self.inserts: list[dict[str, Any]] = []

    def users(self) -> "FakeGmail":
        return self

    def messages(self) -> "FakeGmail":
        return self

    def list(self, **kwargs: Any) -> FakeRequest:
        self.queries.append(kwargs)
        message_id = str(kwargs["q"]).removeprefix("rfc822msgid:")
        found = [{"id": "abc", "threadId": "abc"}] if message_id in self.present else []
        return FakeRequest({"messages": found} if found else {"resultSizeEstimate": 0})

    def insert(self, **kwargs: Any) -> FakeRequest:
        self.inserts.append(kwargs)
        raw = base64.urlsafe_b64decode(kwargs["body"]["raw"])
        message = message_from_bytes(raw, policy=policy.default)
        self.present.add(str(message["Message-ID"]).strip("<>"))
        return FakeRequest({"id": "new"})


def test_insert_skips_a_message_that_is_already_present(folder: Path) -> None:
    messages = load_messages(folder, ACCOUNTS[1])
    gmail = FakeGmail(present={messages[0].message_id})

    report = insert_messages(gmail, messages)

    assert report.skipped == (messages[0].message_id,)
    assert report.inserted == tuple(m.message_id for m in messages[1:])
    assert len(gmail.inserts) == len(messages) - 1


def test_insert_is_safe_to_run_twice(folder: Path) -> None:
    messages = load_messages(folder, ACCOUNTS[0])
    gmail = FakeGmail(present=set())

    first = insert_messages(gmail, messages)
    second = insert_messages(gmail, messages)

    assert len(first.inserted) == len(messages)
    assert second.inserted == ()
    assert len(gmail.inserts) == len(messages)


def test_insert_keeps_the_sender_and_the_date_and_files_in_the_inbox(folder: Path) -> None:
    [message, *_] = load_messages(folder, ACCOUNTS[2])
    gmail = FakeGmail(present=set())

    insert_messages(gmail, [message])

    [call] = gmail.inserts
    assert call["userId"] == "me"
    assert call["internalDateSource"] == "dateHeader"
    assert call["body"]["labelIds"] == ["INBOX"]
    sent = message_from_bytes(base64.urlsafe_b64decode(call["body"]["raw"]), policy=policy.default)
    stored = message_from_bytes(message.raw, policy=policy.default)
    assert str(sent["From"]) == str(stored["From"])
    assert str(sent["Date"]) == str(stored["Date"])
    assert gmail.queries[0]["q"] == f"rfc822msgid:{message.message_id}"


def test_insert_speaks_the_real_gmail_api() -> None:
    from googleapiclient.discovery import (  # pyright: ignore[reportMissingTypeStubs]
        build,  # pyright: ignore[reportUnknownVariableType]
    )
    from googleapiclient.http import (  # pyright: ignore[reportMissingTypeStubs]
        HttpMockSequence,
    )

    from invoice_collector.seed import SeedMessage

    http = HttpMockSequence(
        [
            ({"status": "200"}, json.dumps({"messages": [{"id": "1", "threadId": "1"}]})),
            ({"status": "200"}, json.dumps({"resultSizeEstimate": 0})),
            ({"status": "200"}, json.dumps({"id": "2", "labelIds": ["INBOX"]})),
        ]
    )
    service: Any = build(  # pyright: ignore[reportUnknownVariableType]
        "gmail", "v1", http=http, static_discovery=True
    )
    present = SeedMessage(ACCOUNTS[0], "a.eml", "present@slack.com", b"Subject: a\n\na", {})
    absent = SeedMessage(ACCOUNTS[0], "b.eml", "absent@slack.com", b"Subject: b\n\nb", {})

    report = insert_messages(service, [present, absent])

    assert report.skipped == ("present@slack.com",)
    assert report.inserted == ("absent@slack.com",)
    requests: list[tuple[str, str, str | None, Any]] = http.request_sequence  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    assert [method for _, method, _, _ in requests] == ["GET", "GET", "POST"]
    assert "rfc822msgid%3Apresent%40slack.com" in requests[0][0]
    assert "internalDateSource=dateHeader" in requests[2][0]


@pytest.mark.browser
def test_generate_command_writes_the_folder_with_real_pdfs(tmp_path: Path) -> None:
    folder = tmp_path / "samples"

    exit_code = seed_main(["generate", "--out", str(folder)])

    assert exit_code == 0
    golden: Golden = json.loads((folder / "golden.json").read_text(encoding="utf-8"))
    assert (folder / "expected_vendors.json").is_file()
    assert (folder / "answers.json").is_file()
    assert any((folder / "portal").glob("*.html"))
    size = sum(p.stat().st_size for p in folder.rglob("*") if p.is_file())
    assert size < 6_000_000
    for entry in golden:
        for part in eml_of(folder, entry).iter_attachments():
            content = part.get_payload(decode=True)
            assert isinstance(content, bytes)
            assert content.startswith(b"%PDF-")
            pages = len(re.findall(rb"/Type\s*/Page\b", content))
            if "multi_page" in entry["labels"]:
                assert pages >= 2


@pytest.mark.parametrize("account", ["../other", "nested/account", "portal", ".."])
def test_source_account_that_would_write_outside_its_own_folder_is_refused(
    tmp_path: Path, account: str
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    (other / "kept.eml").write_bytes(b"kept")
    accounts = (account, "ops@nyayalabs.example", "finance@nyayalabs.example")
    seed = generate(SeedConfig(source_accounts=accounts), FakeRenderer())

    with pytest.raises(ValueError, match="source account"):
        write_folder(seed, tmp_path / "samples")

    assert (other / "kept.eml").read_bytes() == b"kept"
