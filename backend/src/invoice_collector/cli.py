"""Command line entry point."""

import argparse
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from invoice_collector.archive import LocalArchive
from invoice_collector.domain import CollectionMonth
from invoice_collector.ledger import Ledger
from invoice_collector.run import collect
from invoice_collector.samples import load_extractor, load_sources
from invoice_collector.summary import CsvSummary


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="invoice-collector")
    commands = parser.add_subparsers(dest="command", required=True)

    collect_cmd = commands.add_parser("collect", help="collect billing documents for a month")
    collect_cmd.add_argument("month", type=CollectionMonth.parse, help="collection month, YYYY-MM")
    collect_cmd.add_argument(
        "--samples", type=Path, required=True, help="folder of sample emails to read"
    )
    collect_cmd.add_argument("--out", type=Path, default=Path("out"), help="where to write output")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    month: CollectionMonth = args.month
    out: Path = args.out
    summary_path = out / f"{month}_summary.csv"

    ledger = Ledger(out / "ledger.sqlite")
    try:
        result = collect(
            month,
            sources=load_sources(args.samples),
            extractor=load_extractor(args.samples),
            archive=LocalArchive(out / "archive"),
            ledger=ledger,
            summary_writers=[CsvSummary(summary_path)],
        )
        states = Counter(e.state.value for e in ledger.examined_emails(month))
    finally:
        ledger.close()

    print(f"Collection month {month}: {len(result.summary)} billing documents collected")
    for state, count in sorted(states.items()):
        print(f"  {state}: {count}")
    print(f"Summary: {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
