"""What the dashboard needs to know before it starts.

Every setting is read from the environment, and every problem with them is found before the
dashboard refuses to start, so whoever deploys it sees them all at once and not one at a time.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit

SESSION_SECRET_VARIABLE = "INVOICE_COLLECTOR_SESSION_SECRET"
ALLOWLIST_VARIABLE = "INVOICE_COLLECTOR_ALLOWLIST"
WEB_CLIENT_FILE_VARIABLE = "INVOICE_COLLECTOR_WEB_CLIENT_FILE"
WEB_CLIENT_ID_VARIABLE = "INVOICE_COLLECTOR_WEB_CLIENT_ID"
WEB_CLIENT_SECRET_VARIABLE = "INVOICE_COLLECTOR_WEB_CLIENT_SECRET"
TOKEN_DIR_VARIABLE = "INVOICE_COLLECTOR_TOKEN_DIR"
SIGN_IN_LIFETIME_VARIABLE = "INVOICE_COLLECTOR_SIGN_IN_LIFETIME_DAYS"
FRONTEND_DIR_VARIABLE = "INVOICE_COLLECTOR_FRONTEND_DIR"
RUNNER_URL_VARIABLE = "INVOICE_COLLECTOR_RUNNER_URL"
RUNNER_SECRET_VARIABLE = "INVOICE_COLLECTOR_RUNNER_SECRET"
PUBLIC_URL_VARIABLE = "INVOICE_COLLECTOR_PUBLIC_URL"
HOST_VARIABLE = "INVOICE_COLLECTOR_DASHBOARD_HOST"
PORT_VARIABLE = "INVOICE_COLLECTOR_DASHBOARD_PORT"

# backend/src/invoice_collector/api/settings.py is four folders below the repo root.
REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_WEB_CLIENT_FILE = REPO_ROOT / "credentials" / "web-client.json"
# Where the command line keeps sign-ins too, so an account connected in one is in the other.
DEFAULT_TOKEN_DIR = REPO_ROOT / "credentials" / "tokens"
# While the OAuth app is in testing, Google lets a refresh token last seven days.
TESTING_SIGN_IN_LIFETIME_DAYS = 7
# The address people open the dashboard at, unless told otherwise: this machine.
DEFAULT_PUBLIC_URL = "http://localhost:8000"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
# The front end's development server (npm run dev), used only when the dashboard does not
# serve the built front end itself.
DEVELOPMENT_FRONTEND_ORIGIN = "http://localhost:5173"


class SettingsError(Exception):
    """The dashboard cannot start with the settings it was given. Each problem is named."""

    def __init__(self, *problems: str) -> None:
        self.problems: tuple[str, ...] = problems
        if len(problems) == 1:
            super().__init__(problems[0])
        else:
            super().__init__("\n".join(f"- {problem}" for problem in problems))


@dataclass(frozen=True)
class Settings:
    session_secret: str
    allowlist: frozenset[str]
    ledger_path: Path
    web_client_file: Path = DEFAULT_WEB_CLIENT_FILE
    # Where people open the dashboard: sign-in comes back here, and cookies are marked
    # secure when it is https.
    public_url: str = DEFAULT_PUBLIC_URL
    frontend_origin: str = DEVELOPMENT_FRONTEND_ORIGIN
    token_dir: Path = DEFAULT_TOKEN_DIR
    # None for a published OAuth app, whose sign-ins last until revoked.
    sign_in_lifetime_days: int | None = TESTING_SIGN_IN_LIFETIME_DAYS
    # The folder `npm run build` writes. When given, the service serves the front end too,
    # so pages and API share one origin; otherwise the front end has a server of its own.
    frontend_dir: Path | None = None
    # The runner service's address on the private network, such as http://runner:8001.
    # When given, runs are asked of it; otherwise this service performs them itself.
    runner_url: str | None = None
    # Sent to the runner with every request. The same value is given to the runner.
    runner_secret: str = ""
    # Where the dashboard listens. In a container, every address of the container.
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT

    @property
    def redirect_uri(self) -> str:
        """Where Google sends a person back after signing in to the dashboard."""
        return f"{self.public_url}/auth/callback"

    @property
    def accounts_redirect_uri(self) -> str:
        """Where Google sends a person back after connecting a source account."""
        return f"{self.public_url}/accounts/callback"

    @property
    def secure_cookies(self) -> bool:
        """Cookies travel only over https when the dashboard is opened over https."""
        return self.public_url.startswith("https://")

    @property
    def own_origins(self) -> frozenset[str]:
        """The origins the dashboard's own pages are served from."""
        return frozenset({origin_of(self.public_url), origin_of(self.frontend_origin)})

    @classmethod
    def from_environment(cls, environment: Mapping[str, str], *, ledger_path: Path) -> "Settings":
        """The settings, or SettingsError naming every problem with them."""
        problems: list[str] = []
        frontend_dir = environment.get(FRONTEND_DIR_VARIABLE, "").strip()
        runner_url = environment.get(RUNNER_URL_VARIABLE, "").strip()
        settings = cls(
            session_secret=environment.get(SESSION_SECRET_VARIABLE, ""),
            allowlist=parse_allowlist(environment.get(ALLOWLIST_VARIABLE, "")),
            ledger_path=ledger_path,
            web_client_file=Path(
                environment.get(WEB_CLIENT_FILE_VARIABLE) or DEFAULT_WEB_CLIENT_FILE
            ),
            public_url=_public_url(environment.get(PUBLIC_URL_VARIABLE, ""), problems),
            token_dir=Path(environment.get(TOKEN_DIR_VARIABLE) or DEFAULT_TOKEN_DIR),
            sign_in_lifetime_days=_collected(
                parse_lifetime, environment.get(SIGN_IN_LIFETIME_VARIABLE, ""), problems
            ),
            frontend_dir=Path(frontend_dir) if frontend_dir else None,
            runner_url=runner_url or None,
            runner_secret=environment.get(RUNNER_SECRET_VARIABLE, ""),
            host=environment.get(HOST_VARIABLE, "").strip() or DEFAULT_HOST,
            port=_collected(
                lambda text: parse_port(text, PORT_VARIABLE, DEFAULT_PORT),
                environment.get(PORT_VARIABLE, ""),
                problems,
            )
            or DEFAULT_PORT,
        )
        if settings.frontend_dir is not None:
            # Served from here, the front end is where sign-in comes back to.
            settings = replace(settings, frontend_origin=origin_of(settings.public_url))
        problems += settings.problems()
        if problems:
            raise SettingsError(*problems)
        return settings

    def problems(self) -> list[str]:
        """Everything that stops the dashboard from starting with these settings."""
        problems: list[str] = []
        if not self.session_secret.strip():
            problems.append(
                f"{SESSION_SECRET_VARIABLE} is not set. The dashboard signs its session cookie "
                "with it and will not start without one. Set it to a long random value."
            )
        if self.runner_url is not None and not self.runner_secret.strip():
            problems.append(
                f"{RUNNER_URL_VARIABLE} is set but {RUNNER_SECRET_VARIABLE} is not. The "
                "runner accepts only requests carrying it: give the dashboard the same value "
                "as the runner."
            )
        if self.runner_url is not None and not self.runner_url.startswith(("http://", "https://")):
            problems.append(
                f"{RUNNER_URL_VARIABLE} must be the runner's address, such as http://runner:8001."
            )
        if self.frontend_dir is not None and not (self.frontend_dir / "index.html").is_file():
            problems.append(
                f"{FRONTEND_DIR_VARIABLE} is {self.frontend_dir}, which holds no built front "
                "end. Run npm run build in frontend/ and give the dist folder it writes."
            )
        return problems

    def check(self) -> None:
        problems = self.problems()
        if problems:
            raise SettingsError(*problems)


def _collected[T](parse: Callable[[str], T], text: str, problems: list[str]) -> T | None:
    """What parse makes of text, or None after adding its problem to the others."""
    try:
        return parse(text)
    except SettingsError as problem:
        problems.extend(problem.problems)
        return None


def origin_of(url: str) -> str:
    scheme, _, rest = url.partition("://")
    return f"{scheme}://{rest.split('/', 1)[0]}"


def _public_url(text: str, problems: list[str]) -> str:
    try:
        return parse_public_url(text)
    except SettingsError as problem:
        problems.extend(problem.problems)
        return DEFAULT_PUBLIC_URL


def parse_public_url(text: str, variable: str = PUBLIC_URL_VARIABLE) -> str:
    """The public address of the dashboard, without a closing slash; the default if unset."""
    text = text.strip()
    if not text:
        return DEFAULT_PUBLIC_URL
    try:
        parts = urlsplit(text)
        parts.port  # noqa: B018 - raises ValueError for a port that is not a number
    except ValueError:
        parts = None
    if (
        parts is None
        or parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
        or parts.username
        or parts.password
    ):
        raise SettingsError(
            f"{variable} must be the address people open the dashboard at, such as "
            f"https://invoices.example.com or {DEFAULT_PUBLIC_URL}, with no path; it is {text}."
        )
    return text.rstrip("/")


def parse_port(text: str, variable: str, default: int) -> int:
    text = text.strip()
    if not text:
        return default
    try:
        port = int(text)
    except ValueError:
        port = 0
    if not 1 <= port <= 65535:
        raise SettingsError(f"{variable} must be a port number from 1 to 65535, not {text}.")
    return port


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
