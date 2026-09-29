"""Command line entry point for the seed data."""

import argparse
import json
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Protocol, cast

from google.auth.exceptions import GoogleAuthError
from google.oauth2.credentials import Credentials
from googleapiclient.errors import Error as GoogleApiError

from invoice_collector import google_auth
from invoice_collector.domain import CollectionMonth
from invoice_collector.google_auth import (
    DEFAULT_CLIENT_FILE,
    DEFAULT_TOKEN_DIR,
    GMAIL_INSERT,
    GMAIL_READONLY,
    GmailService,
    SignInExpired,
)
from invoice_collector.seed.addressing import addressed_to
from invoice_collector.seed.catalogue import DEFAULT_SOURCE_ACCOUNTS
from invoice_collector.seed.generator import (
    Renderer,
    SeedConfig,
    generate,
    load_messages,
    write_folder,
)
from invoice_collector.seed.gmail_insert import InsertedElsewhere, insert_messages
from invoice_collector.seed.hard import generate_hard
from invoice_collector.seed.portal_server import (
    DEFAULT_HOST as PORTAL_HOST,
)
from invoice_collector.seed.portal_server import (
    DEFAULT_PORT,
    portal_server,
    serve_until_interrupted,
)
from invoice_collector.seed.render import BrowserRenderer

# The stored sign-in for inserting is kept apart from the read-only one the pipeline uses,
# as the dashboard keeps it.
SEEDING = google_auth.SEEDING


class SignIn(Protocol):
    def __call__(
        self,
        account: str,
        scopes: Sequence[str],
        token_dir: Path,
        client_file: Path,
        *,
        allow_browser: bool = True,
        renew: bool = False,
        purpose: str | None = None,
        check_address: bool = True,
    ) -> Credentials: ...


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

    hard_cmd = commands.add_parser(
        "hard",
        help="write the hard golden set: hand-written cases real invoices get wrong",
        description="Writes the hard cases for the eval, with their right answers, in the same "
        "layout as the samples. It never touches the samples folder.",
    )
    hard_cmd.add_argument(
        "--out",
        type=Path,
        default=Path("evals/hard"),
        help="folder to write, replacing its emails",
    )

    gmail_cmd = commands.add_parser(
        "gmail",
        help="put the sample emails into real Gmail mailboxes, for a demonstration",
        description="Inserts the sample emails of each sample account into a real mailbox. "
        "Each real account must first be signed in for reading with invoice-collector-setup; "
        "it is then signed in separately, through the browser, with access only to insert mail.",
    )
    gmail_cmd.add_argument(
        "--samples", type=Path, default=Path("samples"), help="folder of sample emails"
    )
    gmail_cmd.add_argument(
        "--map",
        action="append",
        required=True,
        metavar="SAMPLE=REAL",
        help="a sample account and the real address that receives its emails; repeat for each",
    )
    gmail_cmd.add_argument(
        "--portal-base-url",
        default=None,
        help="move portal links to this address when inserting (default: leave them as generated)",
    )
    gmail_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would be inserted, without signing in or inserting",
    )
    gmail_cmd.add_argument("--token-dir", type=Path, default=DEFAULT_TOKEN_DIR)
    gmail_cmd.add_argument("--client-file", type=Path, default=DEFAULT_CLIENT_FILE)

    portal_cmd = commands.add_parser(
        "portal", help="serve the sample portal pages on this machine until interrupted"
    )
    portal_cmd.add_argument(
        "--samples", type=Path, default=Path("samples"), help="folder holding the portal folder"
    )
    portal_cmd.add_argument("--port", type=int, default=DEFAULT_PORT, help="port to serve on")
    portal_cmd.add_argument(
        "--host",
        default=PORTAL_HOST,
        help="address to listen on (default: this machine only); 0.0.0.0 in a container",
    )
    return parser


def _generate(args: argparse.Namespace) -> int:
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


def _hard(
    args: argparse.Namespace, renderer: Callable[[], AbstractContextManager[Renderer]]
) -> int:
    out: Path = args.out
    with renderer() as browser:
        seed = generate_hard(browser)
    write_folder(seed, out)
    counts = Counter(label for m in seed.messages for label in m.golden["labels"])
    emails = len({(m.source_account, m.file_name) for m in seed.messages})
    print(f"Wrote {emails} emails with {len(seed.messages)} golden entries to {out}")
    for label, count in sorted(counts.items()):
        print(f"  {label}: {count}")
    return 0


def _source_accounts_in(golden: object) -> list[str]:
    """The source accounts the sample answers name. Raises ValueError for anything else."""
    if not isinstance(golden, list):
        raise ValueError("not a list of entries")
    accounts: set[str] = set()
    for entry in cast(list[object], golden):
        account = (
            cast(dict[str, object], entry).get("source_account")
            if isinstance(entry, dict)
            else None
        )
        if not isinstance(account, str) or not account.strip():
            raise ValueError("an entry names no source account")
        accounts.add(account)
    return sorted(accounts)


def _mappings(samples: Path, maps: Sequence[str]) -> list[tuple[str, str]] | str:
    """Each sample account with its real address, or why the mappings are refused."""
    golden_file = samples / "golden.json"
    if not golden_file.is_file():
        return f"{samples} holds no sample emails (no golden.json); generate them first"
    try:
        golden: object = json.loads(golden_file.read_text(encoding="utf-8"))
        known = _source_accounts_in(golden)
    except (OSError, ValueError):
        return f"{golden_file} cannot be read as sample answers; generate the samples again"
    mappings: list[tuple[str, str]] = []
    for mapping in maps:
        sample, _, real = mapping.partition("=")
        if not sample or not real:
            return f"--map {mapping}: give it as SAMPLE=REAL"
        if sample not in known:
            return f"{sample} is not a sample account; the samples hold: {', '.join(known)}"
        mappings.append((sample, real))
    return mappings


def _seed_account(
    sample: str,
    real: str,
    args: argparse.Namespace,
    sign_in: SignIn,
    gmail_service: GmailService,
) -> bool:
    messages = [
        addressed_to(m, real, args.portal_base_url) for m in load_messages(args.samples, sample)
    ]
    if args.dry_run:
        print(f"{sample} -> {real}: {len(messages)} messages would be inserted")
        for message in messages:
            print(f"    {message.file_name}")
        return True

    print(f"{sample} -> {real}: if a browser window opens, sign in as {real}.")
    token_dir, client_file = args.token_dir, args.client_file

    def sign_in_to_insert(renew: bool) -> Credentials:
        # The insert scope cannot read the mailbox's address, so the first insert is
        # checked against the read-only sign-in instead.
        return sign_in(
            real,
            [GMAIL_INSERT],
            token_dir,
            client_file,
            renew=renew,
            purpose=SEEDING,
            check_address=False,
        )

    try:
        # Messages are looked up with the read-only sign-in, which is never widened.
        reading = sign_in(real, [GMAIL_READONLY], token_dir, client_file, allow_browser=False)
        try:
            inserting = sign_in_to_insert(renew=False)
        except SignInExpired:
            inserting = sign_in_to_insert(renew=True)
        report = insert_messages(gmail_service(inserting), messages, reader=gmail_service(reading))
    except (
        SignInExpired,
        InsertedElsewhere,
        OSError,
        ValueError,
        GoogleAuthError,
        GoogleApiError,
    ) as error:
        print(f"{real}: NOT seeded. {error}")
        return False
    print(f"{real}: {len(report.inserted)} inserted, {len(report.skipped)} already there")
    return True


def _gmail(args: argparse.Namespace, sign_in: SignIn, gmail_service: GmailService) -> int:
    mappings = _mappings(args.samples, args.map)
    if isinstance(mappings, str):
        print(mappings, file=sys.stderr)
        return 2
    failed = [
        real
        for sample, real in mappings
        if not _seed_account(sample, real, args, sign_in, gmail_service)
    ]
    if failed:
        print(f"Not seeded: {', '.join(failed)}. Run this again once they are signed in.")
        return 1
    return 0


def _portal(args: argparse.Namespace, serve: Callable[[ThreadingHTTPServer], None]) -> int:
    folder: Path = args.samples / "portal"
    if not folder.is_dir():
        print(f"No portal pages in {folder}; generate the samples first", file=sys.stderr)
        return 2
    with portal_server(folder, args.port, args.host) as server:
        print(f"Serving the sample portal pages in {folder}")
        where = "localhost" if args.host == PORTAL_HOST else args.host
        print(f"at http://{where}:{server.server_port}/ until interrupted (Ctrl+C).", flush=True)
        serve(server)
    print("Stopped.")
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    sign_in: SignIn = google_auth.sign_in,
    gmail_service: GmailService = google_auth.gmail_service,
    serve: Callable[[ThreadingHTTPServer], None] = serve_until_interrupted,
    renderer: Callable[[], AbstractContextManager[Renderer]] = BrowserRenderer,
) -> int:
    args = _parser().parse_args(argv)
    if args.command == "gmail":
        return _gmail(args, sign_in, gmail_service)
    if args.command == "portal":
        return _portal(args, serve)
    if args.command == "hard":
        return _hard(args, renderer)
    return _generate(args)


if __name__ == "__main__":
    raise SystemExit(main())
