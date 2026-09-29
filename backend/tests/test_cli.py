"""The command line runs a collection against a folder of sample emails."""

import csv
import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

import pytest
from conftest import ReplayClient
from fake_google import FakeDrive, FakeSheets

from invoice_collector.classifier import FallbackClassifier
from invoice_collector.cli import classifier_for, main, stronger_extractor_for
from invoice_collector.gmail_source import GmailMailSource
from invoice_collector.mail_source import InMemoryMailSource, MailSource
from invoice_collector.mime import email_from_rfc822

PDF = b"%PDF-1.7 figma invoice"


def write_samples(root: Path) -> None:
    message = EmailMessage()
    message["From"] = "Figma <billing@figma.com>"
    message["To"] = "ops@nyayalabs.example"
    message["Subject"] = "Your Figma invoice"
    message["Date"] = format_datetime(datetime(2026, 8, 21, 6, 5, tzinfo=UTC))
    message["Message-ID"] = "<figma-1@figma.com>"
    message.set_content("Your invoice is attached.")
    message.add_attachment(PDF, maintype="application", subtype="pdf", filename="invoice.pdf")

    account = root / "ops@nyayalabs.example"
    account.mkdir(parents=True)
    (account / "figma.eml").write_bytes(bytes(message))
    answers = {
        hashlib.sha256(PDF).hexdigest(): {
            "document_type": "invoice",
            "vendor": "Figma",
            "invoice_date": "2026-08-21",
            "total": "190.00",
            "currency": "USD",
        }
    }
    (root / "answers.json").write_text(json.dumps(answers), encoding="utf-8")


def test_collect_command_produces_an_archive_and_a_summary(tmp_path: Path) -> None:
    samples, out = tmp_path / "samples", tmp_path / "out"
    write_samples(samples)

    exit_code = main(
        ["collect", "2026-08", "--samples", str(samples), "--out", str(out)]
        + ["--extractor", "prepared", "--classifier", "rules", "--no-exchange-rates"]
    )

    assert exit_code == 0
    assert (out / "archive/2026-08/2026-08_Figma_190.00-USD.pdf").read_bytes() == PDF
    with (out / "2026-08_summary.csv").open(newline="", encoding="utf-8") as f:
        [row] = list(csv.DictReader(f))
    assert row["vendor"] == "Figma"
    assert row["source_account"] == "ops@nyayalabs.example"


OWNER = "ops@nyayalabs.example"
OFFLINE = ["--extractor", "prepared", "--classifier", "rules", "--no-exchange-rates"]


def store_owner_sign_in(token_dir: Path) -> None:
    token_dir.mkdir(parents=True)
    (token_dir / f"{OWNER}.json").write_text(
        json.dumps(
            {
                "token": "stored-access-value",
                "refresh_token": "stored-refresh-value",
                "token_uri": "http://127.0.0.1:9/never-called",
                "client_id": "made-up-client.apps.googleusercontent.com",
                "client_secret": "made-up",
                "scopes": ["https://www.googleapis.com/auth/drive.file"],
                "expiry": "2999-01-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )


def test_collect_command_refuses_to_run_when_the_owner_account_is_not_signed_in(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    samples, out = tmp_path / "samples", tmp_path / "out"
    write_samples(samples)

    exit_code = main(
        ["collect", "2026-08", "--samples", str(samples), "--out", str(out)]
        + ["--google-owner", OWNER, "--token-dir", str(tmp_path / "tokens")]
        + OFFLINE
    )

    assert exit_code == 1
    assert f"invoice-collector-setup --owner {OWNER}" in capsys.readouterr().err
    assert not out.exists()


def test_collect_command_with_an_owner_archives_to_drive_and_writes_the_sheet(
    tmp_path: Path,
) -> None:
    samples, out, tokens = tmp_path / "samples", tmp_path / "out", tmp_path / "tokens"
    write_samples(samples)
    store_owner_sign_in(tokens)
    drive = FakeDrive()
    sheets = FakeSheets(drive)

    exit_code = main(
        ["collect", "2026-08", "--samples", str(samples), "--out", str(out)]
        + ["--google-owner", OWNER, "--token-dir", str(tokens)]
        + OFFLINE,
        google_services=lambda credentials: (drive, sheets),
    )

    assert exit_code == 0
    [uploaded] = drive.named("2026-08_Figma_190.00-USD.pdf")
    assert uploaded.content == PDF
    assert (out / "archive/2026-08/2026-08_Figma_190.00-USD.pdf").read_bytes() == PDF
    with (out / "2026-08_summary.csv").open(newline="", encoding="utf-8") as f:
        [row] = list(csv.DictReader(f))
    assert row["file_link"] == uploaded.link
    [sheet] = drive.named("Invoice summary 2026-08")
    assert sheets.tab(sheet.id, "Summary").rows[1][0] == "Figma"


def chain_of(classifier: FallbackClassifier) -> list[str]:
    return [type(c).__name__ for c in classifier.classifiers]


KEYS = {"JEV_API_KEY": "jev-key", "ANTHROPIC_API_KEY": "claude-key"}


def test_jev_classifies_by_default_with_claude_and_rules_behind_it() -> None:
    assert chain_of(classifier_for(None, KEYS)) == [
        "JevClassifier",
        "ClaudeClassifier",
        "RuleClassifier",
    ]


def test_claude_classifies_by_default_when_jev_has_no_key() -> None:
    chain = chain_of(classifier_for(None, {"ANTHROPIC_API_KEY": "claude-key"}))

    assert chain == ["ClaudeClassifier", "RuleClassifier"]


def test_rules_classify_when_no_key_is_present() -> None:
    assert chain_of(classifier_for(None, {})) == ["RuleClassifier"]


def test_classifier_that_is_chosen_is_not_preceded_by_another() -> None:
    assert chain_of(classifier_for("claude", KEYS)) == ["ClaudeClassifier", "RuleClassifier"]


def test_choosing_a_classifier_without_its_key_is_refused() -> None:
    with pytest.raises(SystemExit, match="jev classifier needs its API key"):
        classifier_for("jev", {"ANTHROPIC_API_KEY": "claude-key"})


def test_doubted_readings_go_to_a_stronger_model_when_claude_is_in_use() -> None:
    assert stronger_extractor_for(None, KEYS) is not None


def test_prepared_answers_have_no_stronger_model_behind_them() -> None:
    assert stronger_extractor_for("prepared", KEYS) is None
    assert stronger_extractor_for(None, {}) is None


ENGINEERING, FINANCE = "engineering@nyayalabs.example", "finance@nyayalabs.example"
SLACK_ANSWER = json.loads(
    (Path(__file__).parent / "recorded" / "claude_slack_invoice.json").read_text("utf-8")
)


class Mailboxes:
    """Stands in for reading source accounts through Gmail, remembering what was asked."""

    def __init__(self, not_signed_in: Sequence[str] = ()) -> None:
        self.asked: list[tuple[str, Path]] = []
        self._not_signed_in = set(not_signed_in)

    def __call__(self, account: str, token_dir: Path) -> MailSource:
        self.asked.append((account, token_dir))
        if account in self._not_signed_in:
            # The real source, with no stored sign-in: it fails only when read.
            return GmailMailSource.signed_in(account, token_dir=token_dir)
        message = EmailMessage()
        message["From"] = "Slack <feedback@slack.com>"
        message["To"] = account
        message["Subject"] = "Your Slack invoice"
        message["Date"] = format_datetime(datetime(2026, 8, 3, 6, 5, tzinfo=UTC))
        message["Message-ID"] = f"<slack-1@{account}>"
        message.set_content("Your invoice is attached.")
        message.add_attachment(PDF, maintype="application", subtype="pdf", filename="invoice.pdf")
        email = email_from_rfc822(account, bytes(message))
        return InMemoryMailSource(account, [email])


def collect_from_gmail(
    tmp_path: Path,
    replay_client: ReplayClient,
    mailboxes: Mailboxes,
    *options: str,
) -> int:
    return main(
        ["collect", "2026-08", "--out", str(tmp_path / "out")]
        + ["--token-dir", str(tmp_path / "tokens"), "--no-exchange-rates"]
        + list(options),
        mail_source_for=mailboxes,
        claude_client=lambda: replay_client(200, SLACK_ANSWER),
    )


def summary_rows(out: Path) -> list[dict[str, str]]:
    with (out / "2026-08_summary.csv").open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_collect_command_reads_each_account_through_gmail(
    tmp_path: Path, replay_client: ReplayClient
) -> None:
    mailboxes = Mailboxes()

    exit_code = collect_from_gmail(
        tmp_path, replay_client, mailboxes, "--account", OWNER, "--account", FINANCE
    )

    assert exit_code == 0
    assert mailboxes.asked == [(OWNER, tmp_path / "tokens"), (FINANCE, tmp_path / "tokens")]
    [row] = summary_rows(tmp_path / "out")
    assert row["vendor"] == "Slack"
    assert (tmp_path / "out/archive/2026-08/2026-08_Slack_652.50-USD.pdf").read_bytes() == PDF


def test_account_that_is_not_signed_in_is_reported_and_the_others_are_collected(
    tmp_path: Path, replay_client: ReplayClient, capsys: pytest.CaptureFixture[str]
) -> None:
    mailboxes = Mailboxes(not_signed_in=[ENGINEERING])

    exit_code = collect_from_gmail(
        tmp_path,
        replay_client,
        mailboxes,
        *["--account", ENGINEERING, "--account", OWNER, "--classifier", "rules"],
    )

    assert exit_code == 0
    assert [row["vendor"] for row in summary_rows(tmp_path / "out")] == ["Slack"]
    printed = capsys.readouterr().out
    assert f"Could not read {ENGINEERING}" in printed
    assert f"invoice-collector-setup {ENGINEERING}" in printed


@pytest.mark.parametrize(
    "choice",
    [[], ["--samples", "samples", "--account", OWNER]],
    ids=["neither", "both"],
)
def test_collect_command_needs_either_sample_emails_or_accounts(
    choice: list[str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mailboxes = Mailboxes()

    exit_code = main(
        ["collect", "2026-08", "--out", str(tmp_path / "out"), *choice], mail_source_for=mailboxes
    )

    assert exit_code == 2
    assert "either --samples or --account" in capsys.readouterr().err
    assert mailboxes.asked == []
    assert not (tmp_path / "out").exists()


def test_prepared_answers_are_refused_when_reading_accounts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mailboxes = Mailboxes()

    exit_code = main(
        ["collect", "2026-08", "--out", str(tmp_path / "out")]
        + ["--account", OWNER, "--extractor", "prepared"],
        mail_source_for=mailboxes,
    )

    assert exit_code == 2
    assert "prepared answers exist only for sample emails" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()
