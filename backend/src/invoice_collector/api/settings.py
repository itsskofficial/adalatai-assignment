"""What the dashboard needs to know before it starts."""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

SESSION_SECRET_VARIABLE = "INVOICE_COLLECTOR_SESSION_SECRET"
ALLOWLIST_VARIABLE = "INVOICE_COLLECTOR_ALLOWLIST"
WEB_CLIENT_FILE_VARIABLE = "INVOICE_COLLECTOR_WEB_CLIENT_FILE"
TOKEN_DIR_VARIABLE = "INVOICE_COLLECTOR_TOKEN_DIR"
SIGN_IN_LIFETIME_VARIABLE = "INVOICE_COLLECTOR_SIGN_IN_LIFETIME_DAYS"
FRONTEND_DIR_VARIABLE = "INVOICE_COLLECTOR_FRONTEND_DIR"

# backend/src/invoice_collector/api/settings.py is four folders below the repo root.
REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_WEB_CLIENT_FILE = REPO_ROOT / "credentials" / "web-client.json"
# Where the command line keeps sign-ins too, so an account connected in one is in the other.
DEFAULT_TOKEN_DIR = REPO_ROOT / "credentials" / "tokens"
# While the OAuth app is in testing, Google lets a refresh token last seven days.
TESTING_SIGN_IN_LIFETIME_DAYS = 7


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
    token_dir: Path = DEFAULT_TOKEN_DIR
    accounts_redirect_uri: str = "http://localhost:8000/accounts/callback"
    # None for a published OAuth app, whose sign-ins last until revoked.
    sign_in_lifetime_days: int | None = TESTING_SIGN_IN_LIFETIME_DAYS
    # The folder `npm run build` writes. When given, the service serves the front end too,
    # so pages and API share one origin; otherwise the front end has a server of its own.
    frontend_dir: Path | None = None

    @classmethod
    def from_environment(cls, environment: Mapping[str, str], *, ledger_path: Path) -> "Settings":
        frontend_dir = environment.get(FRONTEND_DIR_VARIABLE, "").strip()
        settings = cls(
            session_secret=environment.get(SESSION_SECRET_VARIABLE, ""),
            allowlist=parse_allowlist(environment.get(ALLOWLIST_VARIABLE, "")),
            ledger_path=ledger_path,
            web_client_file=Path(
                environment.get(WEB_CLIENT_FILE_VARIABLE) or DEFAULT_WEB_CLIENT_FILE
            ),
            token_dir=Path(environment.get(TOKEN_DIR_VARIABLE) or DEFAULT_TOKEN_DIR),
            sign_in_lifetime_days=parse_lifetime(environment.get(SIGN_IN_LIFETIME_VARIABLE, "")),
            frontend_dir=Path(frontend_dir) if frontend_dir else None,
        )
        if settings.frontend_dir is not None:
            # Served from here, the front end is where sign-in comes back to.
            settings = replace(settings, frontend_origin=_origin_of(settings.redirect_uri))
        settings.check()
        return settings

    def check(self) -> None:
        if not self.session_secret.strip():
            raise SettingsError(
                f"{SESSION_SECRET_VARIABLE} is not set. The dashboard signs its session cookie "
                "with it and will not start without one. Set it to a long random value."
            )
        if self.frontend_dir is not None and not (self.frontend_dir / "index.html").is_file():
            raise SettingsError(
                f"{FRONTEND_DIR_VARIABLE} is {self.frontend_dir}, which holds no built front "
                "end. Run npm run build in frontend/ and give the dist folder it writes."
            )


def _origin_of(url: str) -> str:
    scheme, _, rest = url.partition("://")
    return f"{scheme}://{rest.split('/', 1)[0]}"


def normalise(email: str) -> str:
    return email.strip().lower()


def parse_allowlist(text: str) -> frozenset[str]:
    return frozenset(normalise(address) for address in text.split(",") if address.strip())


def parse_lifetime(text: str) -> int | None:
    """Days a sign-in lasts: seven when not set, and none for "off" or 0."""
    text = text.strip().lower()
    if not text:
        return TESTING_SIGN_IN_LIFETIME_DAYS
    if text in ("off", "none", "0"):
        return None
    try:
        days = int(text)
    except ValueError:
        days = 0
    if days < 1:
        raise SettingsError(
            f"{SIGN_IN_LIFETIME_VARIABLE} must be a number of days, or off for a published app."
        )
    return days
