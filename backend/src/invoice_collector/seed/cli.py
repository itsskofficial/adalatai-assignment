"""Command line entry point for the seed data."""

import argparse
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from invoice_collector.domain import CollectionMonth
from invoice_collector.seed.catalogue import DEFAULT_SOURCE_ACCOUNTS
from invoice_collector.seed.generator import SeedConfig, generate, write_folder
from invoice_collector.seed.render import BrowserRenderer


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="invoice-collector-seed")
    commands = parser.add_subparsers(dest="command", required=True)

    generate_cmd = commands.add_parser(
        "generate", help="write the sample emails and their correct answers to a folder"
    )
    generate_cmd.add_argument(
        "--out", type=Path, default=Path("samples"), help="folder to write, replacing its emails"
    )
    generate_cmd.add_argument(
        "--month",
        type=CollectionMonth.parse,
        default=CollectionMonth(2026, 8),
        help="target month, YYYY-MM; the two months before it are the history",
    )
    generate_cmd.add_argument(
        "--source-accounts",
        nargs=3,
        default=list(DEFAULT_SOURCE_ACCOUNTS),
        metavar=("ENGINEERING", "OPS", "FINANCE"),
        help="the three source accounts that receive the emails",
    )
    generate_cmd.add_argument(
        "--portal-base-url",
        default="http://localhost:8765",
        help="where the portal folder will be served",
    )
    generate_cmd.add_argument("--seed", type=int, default=14, help="seed of every random choice")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    engineering, ops, finance = args.source_accounts
    config = SeedConfig(
        target_month=args.month,
        source_accounts=(engineering, ops, finance),
        portal_base_url=args.portal_base_url,
        seed=args.seed,
    )
    out: Path = args.out

    with BrowserRenderer() as renderer:
        seed = generate(config, renderer)
    write_folder(seed, out)

    counts = Counter((m.source_account, m.golden["month"]) for m in seed.messages)
    print(f"Wrote {len(seed.messages)} emails to {out}")
    for (account, month), count in sorted(counts.items()):
        print(f"  {account} {month}: {count}")
    print(f"Portal pages: {len(seed.portal_pages)}, served from {config.portal_base_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
