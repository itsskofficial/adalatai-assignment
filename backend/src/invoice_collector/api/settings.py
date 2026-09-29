"""What the dashboard needs to know before it starts."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

SESSION_SECRET_VARIABLE = "INVOICE_COLLECTOR_SESSION_SECRET"
ALLOWLIST_VARIABLE = "INVOICE_COLLECTOR_ALLOWLIST"
WEB_CLIENT_FILE_VARIABLE = "INVOICE_COLLECTOR_WEB_CLIENT_FILE"

# backend/src/invoice_collector/api/settings.py is four folders below the repo root.
REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_WEB_CLIENT_FILE = REPO_ROOT / "credentials" / "web-client.json"


class SettingsError(Exception):
    """The dashboard cannot start with the settings it was given."""


@dataclass(frozen=True)
class Settings:
    session_secret: str
    allowlist: frozenset[str]
    ledger_path: Path
    web_client_file: Path = DEFAULT_WEB_CLIENT_FILE
    redirect_uri: str = "http://localhost:8000/auth/callback"
    frontend_origin: str = "http://localhost:5173"

    @classmethod
    def from_environment(cls, environment: Mapping[str, str], *, ledger_path: Path) -> "Settings":
        settings = cls(
            session_secret=environment.get(SESSION_SECRET_VARIABLE, ""),
            allowlist=parse_allowlist(environment.get(ALLOWLIST_VARIABLE, "")),
            ledger_path=ledger_path,
            web_client_file=Path(
                environment.get(WEB_CLIENT_FILE_VARIABLE) or DEFAULT_WEB_CLIENT_FILE
            ),
        )
        settings.check()
        return settings

    def check(self) -> None:
        if not self.session_secret.strip():
            raise SettingsError(
                f"{SESSION_SECRET_VARIABLE} is not set. The dashboard signs its session cookie "
                "with it and will not start without one. Set it to a long random value."
            )

    def allows(self, email: str) -> bool:
        return normalise(email) in self.allowlist


def normalise(email: str) -> str:
    return email.strip().lower()


def parse_allowlist(text: str) -> frozenset[str]:
    return frozenset(normalise(address) for address in text.split(",") if address.strip())
