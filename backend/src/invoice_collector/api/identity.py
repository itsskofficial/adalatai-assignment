"""Who a person signing in is, according to Google."""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import urlencode

import httpx
from google.auth import exceptions as google_exceptions
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

from invoice_collector.api.settings import SettingsError

AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
SCOPES = "openid email"


class IdentityNotVerified(Exception):
    """Google did not vouch for the person."""


class IdentityVerifier(Protocol):
    def authorization_url(self, state: str) -> str:
        """Where to send the person to sign in."""
        ...

    def verified_email(self, code: str) -> str:
        """Exchanges the authorization code and returns the email address Google verified.

        Raises IdentityNotVerified when the code or the ID token is not good.
        """
        ...


@dataclass(frozen=True)
class WebClient:
    client_id: str
    client_secret: str = field(repr=False)

    @classmethod
    def read(cls, path: Path) -> "WebClient":
        try:
            loaded: Any = json.loads(path.read_text(encoding="utf-8"))
            web = loaded["web"]
            return cls(client_id=str(web["client_id"]), client_secret=str(web["client_secret"]))
        except FileNotFoundError:
            raise SettingsError(
                f"The web client file {path} does not exist. Download the OAuth client for a "
                "web application from the Google Cloud console and save it there."
            ) from None
        except (KeyError, TypeError, ValueError):
            # The cause is dropped so the file's contents never reach a log.
            raise SettingsError(
                f"The web client file {path} is not a Google web application client."
            ) from None


class GoogleIdentityVerifier:
    """The authorization-code flow for a web application."""

    def __init__(self, client: WebClient, redirect_uri: str) -> None:
        self._client = client
        self._redirect_uri = redirect_uri

    def authorization_url(self, state: str) -> str:
        query = urlencode(
            {
                "client_id": self._client.client_id,
                "redirect_uri": self._redirect_uri,
                "response_type": "code",
                "scope": SCOPES,
                "state": state,
                "prompt": "select_account",
            }
        )
        return f"{AUTHORIZATION_ENDPOINT}?{query}"

    def verified_email(self, code: str) -> str:
        claims = self._verified_claims(self._exchange(code))
        email = claims.get("email")
        if not isinstance(email, str) or not email or claims.get("email_verified") is not True:
            raise IdentityNotVerified("Google has not verified this email address")
        return email

    def _exchange(self, code: str) -> str:
        try:
            response = httpx.post(
                TOKEN_ENDPOINT,
                data={
                    "code": code,
                    "client_id": self._client.client_id,
                    "client_secret": self._client.client_secret,
                    "redirect_uri": self._redirect_uri,
                    "grant_type": "authorization_code",
                },
                timeout=10,
            )
        except httpx.HTTPError:
            raise IdentityNotVerified("Google could not be reached") from None
        if response.status_code != 200:
            raise IdentityNotVerified("Google refused the authorization code")
        try:
            body: Any = response.json()
        except ValueError:
            raise IdentityNotVerified("Google sent an unreadable answer") from None
        token: Any = cast(dict[str, Any], body).get("id_token") if isinstance(body, dict) else None
        if not isinstance(token, str):
            raise IdentityNotVerified("Google sent no ID token")
        return token

    def _verified_claims(self, token: str) -> dict[str, Any]:
        try:
            claims = id_token.verify_oauth2_token(  # pyright: ignore[reportUnknownMemberType]
                token,
                google_requests.Request(),
                audience=self._client.client_id,
                clock_skew_in_seconds=10,
            )
        except (ValueError, google_exceptions.GoogleAuthError):
            raise IdentityNotVerified("the ID token is not valid") from None
        return cast(dict[str, Any], claims)


class FakeIdentityVerifier:
    """Stands in for Google in tests: each known code belongs to one email address."""

    def __init__(self, emails: dict[str, str]) -> None:
        self.emails = emails
        self.codes_exchanged: list[str] = []

    def authorization_url(self, state: str) -> str:
        return f"https://accounts.google.example/auth?{urlencode({'state': state})}"

    def verified_email(self, code: str) -> str:
        self.codes_exchanged.append(code)
        if code not in self.emails:
            raise IdentityNotVerified("unknown authorization code")
        return self.emails[code]
