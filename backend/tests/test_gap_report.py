"""The expected vendor file, and the report of what a run found to be absent."""

import csv
import json
from pathlib import Path

import pytest

from invoice_collector.cli import main
from invoice_collector.domain import Gap
from invoice_collector.gap_report import (
    ExpectedVendorFileInvalid,
    read_expected_vendors,
    write_gaps,
)

BACKEND = Path(__file__).parents[1]


def vendor_file(tmp_path: Path, entries: object) -> Path:
    path = tmp_path / "expected_vendors.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


def test_expected_vendors_are_read_from_a_file(tmp_path: Path) -> None:
    path = vendor_file(
        tmp_path,
        [
            {
                "vendor": "GitHub",
                "source_account": "engineering@nyayalabs.example",
                "billing_cycle": "annual",
                "renewal_month": 8,
                "usual_amount": "2520.00",
                "currency": "USD",
            },
            {"vendor": "Canva"},
        ],
    )

    github, canva = read_expected_vendors(path)

    assert (github.billing_cycle, github.renewal_month) == ("annual", 8)
    assert str(github.usual_amount) == "2520.00"
    assert (canva.billing_cycle, canva.source_account, canva.status) == (
        "monthly",
        None,
        "expected",
    )


@pytest.mark.parametrize(
    "entries",
    [
        {"vendor": "Slack"},
        [{"source_account": "ops@nyayalabs.example"}],
        [{"vendor": "Slack", "billing_cycle": "weekly"}],
        [{"vendor": "Slack", "billing_cycle": "annual"}],
        [{"vendor": "Slack", "billing_cycle": "annual", "renewal_month": 13}],
        [{"vendor": "Slack", "usual_amount": "a lot"}],
    ],
)
def test_file_that_does_not_hold_vendors_is_refused(tmp_path: Path, entries: object) -> None:
    with pytest.raises(ExpectedVendorFileInvalid):
        read_expected_vendors(vendor_file(tmp_path, entries))


def test_the_committed_expected_vendor_file_can_be_read() -> None:
    vendors = read_expected_vendors(BACKEND / "samples/expected_vendors.json")

    assert len(vendors) > 10


def test_gaps_are_written_as_csv(tmp_path: Path) -> None:
    gaps = [
        Gap("Zoom", "missing", "ops@nyayalabs.example", "payment failed on 12 August"),
        Gap("=cmd()", "unknown", None, None),
    ]

    write_gaps(tmp_path / "gaps.csv", gaps)

    with (tmp_path / "gaps.csv").open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert rows == [
        {
            "vendor": "Zoom",
            "gap": "missing",
            "source_account": "ops@nyayalabs.example",
            "explanation": "payment failed on 12 August",
        },
        {"vendor": "'=cmd()", "gap": "unknown", "source_account": "", "explanation": ""},
    ]


def test_collect_command_reports_the_gap_in_the_sample_emails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "out"

    main(
        ["collect", "2026-08", "--samples", str(BACKEND / "samples"), "--out", str(out)]
        + ["--extractor", "prepared", "--classifier", "rules", "--no-exchange-rates"]
    )

    with (out / "2026-08_gaps.csv").open(newline="", encoding="utf-8") as f:
        gaps = {row["vendor"]: row for row in csv.DictReader(f)}
    assert gaps["Zoom"]["gap"] == "missing"
    assert gaps["Zoom"]["explanation"].startswith("payment failed on")
    assert "missing: Zoom (payment failed on" in capsys.readouterr().out
