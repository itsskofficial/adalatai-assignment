"""Connecting a source account through the browser: Google's side of it.

The person is sent to Google to grant read-only access to one mailbox, and comes back
with an authorization code. The code is exchanged for a sign-in, and the address that
signed in is looked up in Gmail, so the dashboard can refuse any address but the one it
asked for. See ADR 0002.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, cast
from urllib.parse import urlencode

import httpx
from google.auth.exceptions import GoogleAuthError
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from google.oauth2.credentials import Credentials
from googleapiclient.errors import Error as GoogleApiError

from invoice_collector import google_auth
from invoice_collector.api.identity import AUTHORIZATION_ENDPOINT, TOKEN_ENDPOINT, WebClient
from invoice_collector.google_auth import GMAIL_INSERT

GmailService = Callable[[Credentials], Any]
# Checks an ID token's signature, issuer and audience, and gives its claims.
VerifyIdToken = Callable[[str, str], Any]


def verified_id_token(token: str, audience: str) -> Any:
    """The claims of an ID token Google signed for this client. Raises ValueError if not."""
    return id_token.verify_oauth2_token(  # pyright: ignore[reportUnknownMemberType]
        token, google_requests.Request(), audience=audience, clock_skew_in_seconds=10
    )


class ConnectionNotCompleted(Exception):
    """Google did not hand over a usable sign-in. The message says why, in plain words."""


@dataclass(frozen=True)
class ConnectedSignIn:
    """A sign-in obtained through the browser, and the address that signed in."""

    address: str
    credentials: Credentials


class SourceAccountConnector(Protocol):
    def authorization_url(self, state: str, address: str, scopes: Sequence[str]) -> str:
        """Where to send the person to grant access to the mailbox at address."""
        ...

    def signed_in(self, code: str, scopes: Sequence[str]) -> ConnectedSignIn:
        """Exchanges the authorization code, and finds out which address signed in.

        Raises ConnectionNotCompleted when the code cannot be exchanged, or no refresh
        token or address comes back.
        """
        ...

    def signed_in_to_insert(self, code: str) -> ConnectedSignIn:
        """Exchanges a code for leave to insert mail, and finds out which address gave it.

        Leave to insert cannot read the mailbox's address from Gmail, so it is asked for
        with the address itself (INSERT_SCOPES), which Google vouches for in its answer.
        Raises ConnectionNotCompleted as signed_in does.
        """
        ...


# Leave to insert mail, and the signed-in address, which the insert scope cannot read.
INSERT_SCOPES = (GMAIL_INSERT, "openid", "email")


class GoogleSourceAccountConnector:
    """The authorization-code flow for a web application, asking for offline access."""

    def __init__(
        self,
        client: WebClient,
        redirect_uri: str,
        *,
        token_endpoint: str = TOKEN_ENDPOINT,
        gmail_service: GmailService = google_auth.gmail_service,
        verify_id_token: VerifyIdToken = verified_id_token,
    ) -> None:
        self._client = client
        self._redirect_uri = redirect_uri
        self._token_endpoint = token_endpoint
        self._gmail_service = gmail_service
        self._verify_id_token = verify_id_token

    def authorization_url(self, state: str, address: str, scopes: Sequence[str]) -> str:
        query = urlencode(
            {
                "client_id": self._client.client_id,
                "redirect_uri": self._redirect_uri,
                "response_type": "code",
                "scope": " ".join(scopes),
                "state": state,
                # A refresh token is issued only for offline access, and only when the
                # person is asked to consent again.
                "access_type": "offline",
                "prompt": "consent",
                "login_hint": address,
            }
        )
        return f"{AUTHORIZATION_ENDPOINT}?{query}"

    def signed_in(self, code: str, scopes: Sequence[str]) -> ConnectedSignIn:
        credentials, _ = self._exchange(code, scopes)
        try:
            profile = self._gmail_service(credentials).users().getProfile(userId="me").execute()
            address = str(profile["emailAddress"])
        except (GoogleApiError, GoogleAuthError, OSError, KeyError, TypeError):
            raise ConnectionNotCompleted("Gmail did not say which address signed in") from None
        return ConnectedSignIn(address=address, credentials=credentials)

    def signed_in_to_insert(self, code: str) -> ConnectedSignIn:
        credentials, answer = self._exchange(code, INSERT_SCOPES)
        token = answer.get("id_token")
        try:
            claims: Any = self._verify_id_token(
                token if isinstance(token, str) else "", self._client.client_id
            )
        except (ValueError, GoogleAuthError):
            raise ConnectionNotCompleted("Google did not say which address signed in") from None
        email = cast(dict[str, Any], claims).get("email") if isinstance(claims, dict) else None
        verified = cast(dict[str, Any], claims).get("email_verified") if email else None
        if not isinstance(email, str) or not email or verified is not True:
            raise ConnectionNotCompleted("Google has not verified the address that signed in")
        return ConnectedSignIn(address=email, credentials=credentials)

    def _exchange(self, code: str, scopes: Sequence[str]) -> tuple[Credentials, dict[str, Any]]:
        # No error below carries its cause: the answer holds tokens, which never reach a log.
        try:
            response = httpx.post(
                self._token_endpoint,
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
            raise ConnectionNotCompleted("Google could not be reached") from None
        if response.status_code != 200:
            raise ConnectionNotCompleted("Google refused the authorization code")
        try:
            body: Any = response.json()
        except ValueError:
            raise ConnectionNotCompleted("Google sent an unreadable answer") from None
        answer = cast(dict[str, Any], body) if isinstance(body, dict) else {}
        access_token = answer.get("access_token")
        refresh_token = answer.get("refresh_token")
        if not isinstance(access_token, str) or not isinstance(refresh_token, str):
            raise ConnectionNotCompleted("Google gave no lasting sign-in")
        granted = answer.get("scope")
        expires_in = answer.get("expires_in")
        credentials = Credentials(  # pyright: ignore[reportUnknownVariableType]
            token=access_token,
            refresh_token=refresh_token,
            token_uri=self._token_endpoint,
            client_id=self._client.client_id,
            client_secret=self._client.client_secret,
            scopes=granted.split() if isinstance(granted, str) else list(scopes),
            # The library compares against a UTC time without a zone.
            expiry=(datetime.now(UTC) + timedelta(seconds=int(expires_in))).replace(tzinfo=None)
            if isinstance(expires_in, int)
            else None,
        )
        return credentials, answer


FAKE_CLIENT_SECRET = "fake-web-client-secret"


def fake_access_token(code: str) -> str:
    return f"fake-access-token-for-{code}"


def fake_refresh_token(code: str) -> str:
    return f"fake-refresh-token-for-{code}"


class FakeSourceAccountConnector:
    """Stands in for Google in tests: each known code is a sign-in by one address.

    Its sign-ins last far into the future, so using one never calls Google.
    """

    def __init__(self, addresses: dict[str, str]) -> None:
        self.addresses = addresses
        self.codes_exchanged: list[str] = []

    def authorization_url(self, state: str, address: str, scopes: Sequence[str]) -> str:
        query = urlencode({"state": state, "login_hint": address, "scope": " ".join(scopes)})
        return f"https://accounts.google.example/auth?{query}"

    def signed_in_to_insert(self, code: str) -> ConnectedSignIn:
        return self.signed_in(code, INSERT_SCOPES)

    def signed_in(self, code: str, scopes: Sequence[str]) -> ConnectedSignIn:
        self.codes_exchanged.append(code)
        if code not in self.addresses:
            raise ConnectionNotCompleted("Google refused the authorization code")
        credentials = Credentials(  # pyright: ignore[reportUnknownVariableType]
            token=fake_access_token(code),
            refresh_token=fake_refresh_token(code),
            token_uri="http://127.0.0.1:9/never-called",
            client_id="fake-web-client.apps.googleusercontent.com",
            client_secret=FAKE_CLIENT_SECRET,
            scopes=list(scopes),
            expiry=datetime(2999, 1, 1),
        )
        return ConnectedSignIn(address=self.addresses[code], credentials=credentials)
