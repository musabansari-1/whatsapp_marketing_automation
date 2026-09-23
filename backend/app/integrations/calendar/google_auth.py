"""Google Calendar authentication ([D19], [D62]).

Two ways to prove identity to Google, both behind one interface so
`GoogleCalendarProvider` never branches on which is configured.

**`oauth_refresh_token`** — the Phase 11 arrangement. An operations account consents
once, its refresh token lives in Secret Manager ([D19]), and the worker exchanges it for
an access token. Still supported, and still the local-development path, but it has an
operational cost that only shows up in production: an OAuth consent screen in *Testing*
status issues refresh tokens that **expire after 7 days**, so a personal-Gmail
deployment loses booking every week and needs a human to consent again. Leaving Testing
means Google's sensitive-scope review, and even then the grant is a human's — a password
change or a revoked session kills it.

**`service_account`** ([D62]) — the production arrangement. The worker authenticates as
*itself*, using the identity Cloud Run already gives it (Application Default
Credentials), and each product's calendar is shared with that service account. There is
no refresh token, so nothing expires, nothing is written back, and no human consent
stands between the platform and a booking. Google requires no verification for this
because the service account reads only data shared with it, not user data.

`google.auth` is imported lazily and used through `asyncio.to_thread`, the same way
`GoogleSecretManagerStore` handles the Secret Manager client: the library is synchronous,
and neither local development nor the test suite should need GCP to import this module.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.core.errors import NonRetryableProviderError, ProviderError, RetryableProviderError
from app.core.logging import get_logger
from app.integrations.secrets import SecretStore

logger = get_logger(__name__)

# Read and write: the platform both reads free/busy and creates the demo event. The same
# scope `deploy/oauth-consent.md` asks a human for, so the two paths see the same
# calendar.
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"

TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - an endpoint, not a secret


class CalendarAuthError(NonRetryableProviderError):
    """Authentication cannot succeed without an operator changing something."""


class CalendarAuthenticator(ABC):
    """Mints access tokens for the Calendar API.

    The provider asks for a token before each call and never sees how one is obtained:
    no expiry arithmetic, no ceremony, no secret handling outside this module.
    """

    name: str

    @abstractmethod
    async def access_token(self) -> str:
        """A valid access token, minted if the cached one has expired."""


class ServiceAccountAuthenticator(CalendarAuthenticator):
    """Access tokens from the worker's own Google identity ([D62]).

    Application Default Credentials are resolved once and then refreshed in place. On
    Cloud Run that is the service identity of the running revision — the email named in
    `deploy/setup-infrastructure.sh` as `WORKER_SA` — and every product's calendar must be
    shared with it. Locally, `gcloud auth application-default login` supplies a user's
    credentials instead, which is why this is also the development path when a real
    calendar is wanted.

    Nothing here reads a secret: there is no refresh token to store, rotate or lose.
    """

    name = "google_service_account"

    def __init__(
        self,
        *,
        scopes: Sequence[str] = (CALENDAR_SCOPE,),
        timeout_seconds: float = 20.0,
        credentials: Any | None = None,
        request: Any | None = None,
    ) -> None:
        self._scopes = tuple(scopes)
        self._timeout = timeout_seconds
        # Both injectable so a test proves the whole lifecycle — minting, caching and
        # failure classification — with no GCP credential and no socket.
        self._credentials = credentials
        self._request = request

        self._access_token: str | None = None
        self._expires_at: datetime | None = None
        # Serialises refreshes: several concurrent turns would otherwise each mint a
        # token, and the extra ones are pure waste against a quota.
        self._token_lock = asyncio.Lock()

    async def access_token(self) -> str:
        async with self._token_lock:
            # Refreshed a minute early: a token that expires mid-request would fail a
            # booking the customer is waiting on.
            if self._access_token and self._expires_at and datetime.now(UTC) < self._expires_at:
                return self._access_token

            token, expires_at = await self._mint_with_timeout()
            self._access_token = token
            self._expires_at = expires_at
            return token

    async def _mint_with_timeout(self) -> tuple[str, datetime]:
        # Bound from here, not below it: google-auth's token call takes no timeout, and
        # a hung identity endpoint must fail a turn retryably rather than stall it.
        try:
            return await asyncio.wait_for(asyncio.to_thread(self._mint), timeout=self._timeout)
        except TimeoutError as exc:
            raise RetryableProviderError(
                f"Token request timed out after {self._timeout:g}s", provider=self.name
            ) from exc

    # ---------------------------------------------------------------- internals

    def _mint(self) -> tuple[str, datetime]:
        """Synchronous, and only ever called through `asyncio.to_thread`."""
        from google.auth import exceptions as google_exceptions
        from google.auth.transport import requests as google_requests

        try:
            credentials = self._credentials or self._load_credentials()
            # google.auth's transport carries no timeout of its own; the outer
            # wait_for in `access_token` is what bounds a hung identity endpoint.
            credentials.refresh(self._request or google_requests.Request())
        except google_exceptions.RefreshError as exc:
            # The credential was revoked, disabled or deleted. Retrying with the same
            # identity fails identically, so the message says what to fix.
            raise CalendarAuthError(
                f"Google refused the credentials of the service account: {exc}. The "
                "service account must still exist and be enabled; for a user's own "
                "credentials run `gcloud auth application-default login` again.",
                provider=self.name,
            ) from exc
        except google_exceptions.TransportError as exc:
            raise RetryableProviderError(
                f"Could not reach Google to obtain a token: {exc}", provider=self.name
            ) from exc
        except ProviderError:
            # Our own classification — an absent credential, say — must not be re-labelled
            # as transient by the catch-all below.
            raise
        except Exception as exc:  # noqa: BLE001 - unclassified failures are transient
            raise RetryableProviderError(
                f"Token request failed: {type(exc).__name__}: {exc}", provider=self.name
            ) from exc

        token = getattr(credentials, "token", None)
        if not token:
            raise CalendarAuthError("Google returned no access token", provider=self.name)

        expiry = getattr(credentials, "expiry", None)
        if isinstance(expiry, datetime):
            # google-auth reports expiry as a naive UTC instant; treating it as local
            # time would refresh late by the machine's offset.
            aware = expiry if expiry.tzinfo else expiry.replace(tzinfo=UTC)
            remaining = (aware - datetime.now(UTC)).total_seconds()
        else:  # pragma: no cover - every google-auth credential sets expiry
            remaining = 3600.0

        # Never so short that the token dies mid-request.
        expires_at = datetime.now(UTC) + timedelta(seconds=max(remaining - 60, 30))
        return str(token), expires_at

    def _load_credentials(self) -> Any:
        from google.auth import default as google_default
        from google.auth import exceptions as google_exceptions

        try:
            credentials, _project = google_default(scopes=list(self._scopes))
        except google_exceptions.DefaultCredentialsError as exc:
            raise CalendarAuthError(
                "No Google credentials are available in this environment, so calendar "
                "access cannot be authenticated. On Cloud Run the running service account "
                "is used; locally run `gcloud auth application-default login`.",
                provider=self.name,
            ) from exc

        self._credentials = credentials
        return credentials


class OAuthRefreshTokenAuthenticator(CalendarAuthenticator):
    """Access tokens from an operations account's refresh token ([D19]).

    The token itself lives in Secret Manager and is read on first use, never at
    construction, so building a provider costs nothing and touches no secret.

    Google can return a *rotated* refresh token. A worker that does not write the new one
    back authenticates fine until the old token expires and then fails everywhere at
    once, which is why the secret store writes as well as reads and why the worker's
    service account needs `secretmanager.secretVersionAdder`.

    Both failure shapes are named for what an operator must do. [D62] replaced this
    arrangement as the *production* path: a consent screen in Testing status expires the
    refresh token every 7 days, so a personal-Gmail deployment loses booking weekly.
    """

    name = "google_oauth_refresh_token"

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        secret_store: SecretStore,
        secret_name: str,
        timeout_seconds: float = 20.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._secrets = secret_store
        # Which secret, never the value. A product may name its own for a calendar in a
        # different Google account ([D43]).
        self._refresh_token_secret_name = secret_name
        self._timeout = timeout_seconds
        # Injectable so tests supply a mock transport and no request reaches Google.
        self._client = client

        self._refresh_token: str | None = None
        self._access_token: str | None = None
        self._access_token_expires_at: datetime | None = None
        self._token_lock = asyncio.Lock()

    async def access_token(self) -> str:
        async with self._token_lock:
            # Refreshed a minute early (see the expiry calculation below): a token
            # that expires mid-request would fail a booking the customer is waiting on.
            if (
                self._access_token
                and self._access_token_expires_at
                and datetime.now(UTC) < self._access_token_expires_at
            ):
                return self._access_token

            refresh_token = await self._get_refresh_token()
            token, expires_at, rotated = await self._exchange_refresh_token(refresh_token)

            self._access_token = token
            self._access_token_expires_at = expires_at

            if rotated and rotated != refresh_token:
                # [D19]: write the rotation back, or authentication works until the
                # old token expires and then fails everywhere at once.
                self._refresh_token = rotated
                await self._secrets.set_secret(self._refresh_token_secret_name, rotated)
                logger.info("calendar_refresh_token_rotated")

            return token

    # ---------------------------------------------------------------- internals

    async def _get_refresh_token(self) -> str:
        if self._refresh_token:
            return self._refresh_token

        token = await self._secrets.get_secret(self._refresh_token_secret_name)
        if not token:
            # Nothing to retry: someone must complete the one-time OAuth consent and
            # store the result ([D19]).
            raise CalendarAuthError(
                f"No Google refresh token in secret {self._refresh_token_secret_name!r}. "
                "Complete the OAuth consent flow and store the refresh token, or set "
                "GOOGLE_CALENDAR_AUTH_MODE=service_account and share the calendar with the "
                "service account instead.",
                provider=self.name,
            )

        self._refresh_token = token
        return token

    async def _exchange_refresh_token(self, refresh_token: str) -> tuple[str, datetime, str | None]:
        data = {
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }

        try:
            response = await self._post_token(data)
        except httpx.TimeoutException as exc:
            raise RetryableProviderError(
                f"Token request timed out: {exc}", provider=self.name
            ) from exc
        except httpx.HTTPError as exc:
            raise RetryableProviderError(
                f"Token request failed: {exc}", provider=self.name
            ) from exc

        if not response.is_success:
            detail = error_detail(response)
            if response.status_code in (400, 401):
                # invalid_grant: the token was revoked or expired. Retrying cannot
                # help; the consent flow must be run again.
                raise CalendarAuthError(
                    f"Google rejected the refresh token: {detail}", provider=self.name
                )
            raise RetryableProviderError(
                f"Token exchange failed ({response.status_code}): {detail}",
                provider=self.name,
            )

        body = response.json()
        access_token = body.get("access_token")
        if not access_token:
            raise CalendarAuthError("Google returned no access token", provider=self.name)

        expires_in = int(body.get("expires_in") or 3600)
        expires_at = datetime.now(UTC) + timedelta(seconds=max(expires_in - 60, 30))

        return access_token, expires_at, body.get("refresh_token")

    async def _post_token(self, data: dict[str, str]) -> httpx.Response:
        if self._client is not None:
            return await self._client.post(TOKEN_URL, data=data, timeout=self._timeout)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await client.post(TOKEN_URL, data=data)


def error_detail(response: httpx.Response) -> str:
    """A short, credential-free description of a Google error response.

    Shared by the token exchange and the calendar calls themselves: both parse the same
    shapes Google uses, and both must keep a secret out of the log store (§19).
    """
    try:
        body = response.json()
    except ValueError:
        return response.text[:300]

    error = body.get("error")
    if isinstance(error, dict):
        message = error.get("message", "")
        reasons = ",".join(
            item.get("reason", "") for item in error.get("errors", []) if isinstance(item, dict)
        )
        return f"{message} {reasons}".strip()[:300]
    if isinstance(error, str):
        description = body.get("error_description", "")
        return f"{error} {description}".strip()[:300]

    return str(body)[:300]


__all__ = [
    "CALENDAR_SCOPE",
    "TOKEN_URL",
    "CalendarAuthError",
    "CalendarAuthenticator",
    "OAuthRefreshTokenAuthenticator",
    "ServiceAccountAuthenticator",
    "error_detail",
]
