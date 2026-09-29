"""The seed command puts the sample emails into real mailboxes and serves the portal pages.

Google is never called: signing in and the Gmail API are stood in for.
"""

import base64
import threading
import urllib.request
from collections.abc import Sequence
from email import message_from_bytes, policy
from email.message import EmailMessage
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httplib2
import pytest
from google.oauth2.credentials import Credentials
from googleapiclient.errors import HttpError

from invoice_collector.google_auth import GMAIL_INSERT, GMAIL_READONLY, NotSignedIn
from invoice_collector.seed import load_messages
from invoice_collector.seed.cli import main

SAMPLES = Path(__file__).parent.parent / "samples"
ENGINEERING, OPS = "engineering@nyayalabs.example", "ops@nyayalabs.example"
REAL_1, REAL_2 = "real.one@gmail.test", "real.two@gmail.test"
TOKENS, CLIENT = Path("tokens"), Path("client.json")


def http_error(status: int) -> HttpError:
    return HttpError(httplib2.Response({"status": str(status)}), b"{}")


class Request:
    def __init__(self, answer: Any) -> None:
        self._answer = answer

    def execute(self) -> Any:
        if isinstance(self._answer, Exception):
            raise self._answer
        return self._answer


class Mailbox:
    """One real mailbox: what it holds, and the messages inserted into it."""

    def __init__(self, present: Sequence[str] = ()) -> None:
        self.present = set(present)
        self.gmail_ids: set[str] = set()
        self.inserted: list[EmailMessage] = []

    def reader(self) -> "Service":
        return Service(self, may_insert=False)

    def inserter(self) -> "Service":
        return Service(self, may_insert=True)


class Service:
    """A Gmail API client for a mailbox, allowed either to read it or only to insert into it."""

    def __init__(self, mailbox: Mailbox, *, may_insert: bool) -> None:
        self._mailbox = mailbox
        self._may_insert = may_insert

    def users(self) -> "Service":
        return self

    def messages(self) -> "Service":
        return self

    def list(self, **kwargs: Any) -> Request:
        if self._may_insert:
            return Request(http_error(403))
        message_id = str(kwargs["q"]).removeprefix("rfc822msgid:")
        found = message_id in self._mailbox.present
        return Request({"messages": [{"id": "x"}]} if found else {"resultSizeEstimate": 0})

    def get(self, **kwargs: Any) -> Request:
        if self._may_insert:
            return Request(http_error(403))
        if kwargs["id"] not in self._mailbox.gmail_ids:
            return Request(http_error(404))
        return Request({"id": kwargs["id"]})

    def insert(self, **kwargs: Any) -> Request:
        if not self._may_insert:
            return Request(http_error(403))
        raw = base64.urlsafe_b64decode(kwargs["body"]["raw"])
        message = message_from_bytes(raw, policy=policy.default)
        assert isinstance(message, EmailMessage)
        self._mailbox.inserted.append(message)
        self._mailbox.present.add(str(message["Message-ID"]).strip("<>"))
        gmail_id = f"g{len(self._mailbox.inserted)}"
        self._mailbox.gmail_ids.add(gmail_id)
        return Request({"id": gmail_id})


class Google:
    """Stands in for signing in and for building Gmail clients."""

    def __init__(
        self,
        mailboxes: dict[str, Mailbox],
        not_signed_in: Sequence[str] = (),
        inserts_land_in: dict[str, str] | None = None,
    ) -> None:
        self.mailboxes = mailboxes
        self.asked: list[dict[str, Any]] = []
        self._not_signed_in = set(not_signed_in)
        self._inserts_land_in = inserts_land_in or {}

    def sign_in(
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
    ) -> Credentials:
        self.asked.append(
            {
                "account": account,
                "scopes": list(scopes),
                "purpose": purpose,
                "allow_browser": allow_browser,
                "check_address": check_address,
            }
        )
        if account in self._not_signed_in and not allow_browser:
            raise NotSignedIn(account)
        kind = "insert" if GMAIL_INSERT in scopes else "read"
        return Credentials(token=f"{account}|{kind}")  # pyright: ignore[reportUnknownVariableType]

    def gmail_service(self, credentials: Credentials) -> Any:
        token = str(credentials.token)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        account, kind = token.split("|")
        if kind == "insert":
            return self.mailboxes[self._inserts_land_in.get(account, account)].inserter()
        return self.mailboxes[account].reader()


def seed(google: Google, *options: str) -> int:
    return main(
        ["gmail", "--samples", str(SAMPLES), "--token-dir", str(TOKENS)]
        + ["--client-file", str(CLIENT), *options],
        sign_in=google.sign_in,
        gmail_service=google.gmail_service,
    )


MAPS = ["--map", f"{ENGINEERING}={REAL_1}", "--map", f"{OPS}={REAL_2}"]


def test_each_mapped_account_gets_its_sample_emails_addressed_to_the_real_address(
    capsys: pytest.CaptureFixture[str],
) -> None:
    google = Google({REAL_1: Mailbox(), REAL_2: Mailbox()})

    exit_code = seed(google, *MAPS)

    assert exit_code == 0
    printed = capsys.readouterr().out
    for sample, real in ((ENGINEERING, REAL_1), (OPS, REAL_2)):
        expected = load_messages(SAMPLES, sample)
        inserted = google.mailboxes[real].inserted
        assert [str(m["Message-ID"]).strip("<>") for m in inserted] == [
            m.message_id for m in expected
        ]
        assert {str(m["To"]) for m in inserted} == {real}
        assert f"{real}: {len(expected)} inserted, 0 already there" in printed


def test_messages_already_in_the_mailbox_are_not_inserted_again(
    capsys: pytest.CaptureFixture[str],
) -> None:
    first, *rest = load_messages(SAMPLES, OPS)
    google = Google({REAL_2: Mailbox(present=[first.message_id])})

    seed(google, "--map", f"{OPS}={REAL_2}")
    exit_code = seed(google, "--map", f"{OPS}={REAL_2}")

    assert exit_code == 0
    assert len(google.mailboxes[REAL_2].inserted) == len(rest)
    printed = capsys.readouterr().out
    assert f"{REAL_2}: {len(rest)} inserted, 1 already there" in printed
    assert f"{REAL_2}: 0 inserted, {len(rest) + 1} already there" in printed


def test_inserting_uses_a_separate_sign_in_with_only_the_insert_scope() -> None:
    google = Google({REAL_1: Mailbox()})

    seed(google, "--map", f"{ENGINEERING}={REAL_1}")

    inserting = [a for a in google.asked if GMAIL_INSERT in a["scopes"]]
    assert [(a["scopes"], a["purpose"]) for a in inserting] == [([GMAIL_INSERT], "seeding")]
    # Messages are looked up with the stored read-only sign-in, which is never widened.
    reading = [a for a in google.asked if GMAIL_INSERT not in a["scopes"]]
    assert [(a["scopes"], a["purpose"], a["allow_browser"]) for a in reading] == [
        ([GMAIL_READONLY], None, False)
    ]


def test_account_not_signed_in_for_reading_is_not_seeded_and_the_others_are(
    capsys: pytest.CaptureFixture[str],
) -> None:
    google = Google({REAL_1: Mailbox(), REAL_2: Mailbox()}, not_signed_in=[REAL_1])

    exit_code = seed(google, *MAPS)

    assert exit_code == 1
    assert google.mailboxes[REAL_1].inserted == []
    assert google.mailboxes[REAL_2].inserted
    printed = capsys.readouterr().out
    assert f"{REAL_1}: NOT seeded" in printed
    assert f"invoice-collector-setup {REAL_1}" in printed


def test_inserting_into_another_mailbox_than_the_one_read_stops_that_account(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The person signed in for inserting as the wrong address.
    google = Google({REAL_1: Mailbox(), REAL_2: Mailbox()}, inserts_land_in={REAL_1: REAL_2})

    exit_code = seed(google, "--map", f"{ENGINEERING}={REAL_1}")

    assert exit_code == 1
    assert len(google.mailboxes[REAL_2].inserted) == 1
    assert f"{REAL_1}: NOT seeded" in capsys.readouterr().out


def test_dry_run_signs_nothing_in_and_inserts_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    google = Google({REAL_1: Mailbox()})

    exit_code = seed(google, "--map", f"{ENGINEERING}={REAL_1}", "--dry-run")

    assert exit_code == 0
    assert google.asked == []
    assert google.mailboxes[REAL_1].inserted == []
    printed = capsys.readouterr().out
    expected = load_messages(SAMPLES, ENGINEERING)
    assert f"{REAL_1}: {len(expected)} messages would be inserted" in printed
    assert expected[0].file_name in printed


def test_unknown_sample_account_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    google = Google({REAL_1: Mailbox()})

    exit_code = seed(google, "--map", f"nobody@nyayalabs.example={REAL_1}")

    assert exit_code == 2
    assert google.asked == []
    assert "nobody@nyayalabs.example is not a sample account" in capsys.readouterr().err


@pytest.mark.parametrize(
    "golden",
    [
        '[{"source_account": "engi',
        '[{"month": "2026-08"}]',
        '[{"source_account": 3}]',
        '[{"source_account": ""}]',
        '["engineering@nyayalabs.example"]',
        '{"source_account": "engineering@nyayalabs.example"}',
        "7",
    ],
)
def test_sample_answers_that_cannot_be_read_are_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], golden: str
) -> None:
    (tmp_path / "golden.json").write_text(golden, encoding="utf-8")
    google = Google({REAL_1: Mailbox()})

    exit_code = main(
        ["gmail", "--samples", str(tmp_path), "--token-dir", str(TOKENS)]
        + ["--client-file", str(CLIENT), "--map", f"{ENGINEERING}={REAL_1}"],
        sign_in=google.sign_in,
        gmail_service=google.gmail_service,
    )

    assert exit_code == 2
    assert google.asked == []
    assert "cannot be read as sample answers" in capsys.readouterr().err


def test_mapping_without_an_equals_sign_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = seed(Google({}), "--map", REAL_1)

    assert exit_code == 2
    assert "SAMPLE=REAL" in capsys.readouterr().err


def text_bodies(message: EmailMessage) -> str:
    return "".join(
        str(part.get_content())
        for part in message.walk()
        if part.get_content_maintype() == "text" and not part.is_attachment()
    )


def test_portal_links_are_moved_to_the_given_base_url() -> None:
    google = Google({REAL_2: Mailbox()})

    seed(google, "--map", f"{OPS}={REAL_2}", "--portal-base-url", "http://127.0.0.1:9000/")

    bodies = [text_bodies(m) for m in google.mailboxes[REAL_2].inserted]
    with_links = [b for b in bodies if "127.0.0.1:9000" in b]
    assert with_links
    assert "http://127.0.0.1:9000/sign-in.html" in "".join(with_links)
    assert not any("localhost:8765" in b for b in bodies)


def test_portal_links_are_left_as_generated_by_default() -> None:
    google = Google({REAL_2: Mailbox()})

    seed(google, "--map", f"{OPS}={REAL_2}")

    bodies = [text_bodies(m) for m in google.mailboxes[REAL_2].inserted]
    assert any("http://localhost:8765/sign-in.html" in b for b in bodies)


def test_portal_command_serves_the_sample_portal_pages(
    capsys: pytest.CaptureFixture[str],
) -> None:
    started: list[ThreadingHTTPServer] = []
    ready = threading.Event()

    def serve(server: ThreadingHTTPServer) -> None:
        started.append(server)
        ready.set()
        server.serve_forever()

    thread = threading.Thread(
        target=main,
        args=(["portal", "--samples", str(SAMPLES), "--port", "0"],),
        kwargs={"serve": serve},
        daemon=True,
    )
    thread.start()
    assert ready.wait(10)
    [server] = started
    try:
        address = f"http://127.0.0.1:{server.server_port}/sign-in.html"
        with urllib.request.urlopen(address, timeout=10) as response:
            page = response.read()
    finally:
        server.shutdown()
        thread.join(10)

    assert page == (SAMPLES / "portal" / "sign-in.html").read_bytes()
    assert f"http://localhost:{server.server_port}/" in capsys.readouterr().out
