"""Google Calendar provider (specification §12, §20, [D7], [D19]).

The real implementation behind `CalendarProvider`. One shared sales calendar ([D7]);
per-salesperson calendars are not V1.

Spoken to over plain HTTPS rather than through `google-api-python-client`. Two reasons:
the client library is synchronous and would need a thread for every call, and a mock
transport is the only way to test this without a real calendar. The API surface used
here is three endpoints.

**Credentials come from Secret Manager, never from configuration ([D19]).** The refresh
token is read at first use and cached in memory for the process lifetime, which keeps
the worker stateless. When Google returns a rotated refresh token it is written back as
a new secret version — miss that, and authentication keeps working until the old token
expires, then fails everywhere at once.

Idempotency (§12, §20) rests on Google accepting a client-supplied event ID. A retried
booking sends the same ID; Google answers 409, and the provider fetches the existing
event and returns it. A retry therefore converges on one event rather than
double-booking the sales team.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.errors import NonRetryableProviderError, RetryableProviderError
from app.core.logging import get_logger
from app.integrations.calendar.provider import (
    CalendarEvent,
    CalendarProvider,
    EventRequest,
    SlotUnavailableError,
    TimeInterval,
)
from app.integrations.secrets import SecretStore

logger = get_logger(__name__)

CALENDAR_API = "https://www.googleapis.com/calendar/v3"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - an endpoint, not a secret


class GoogleCalendarProvider(CalendarProvider):
    """Reads availability from and writes events to one Google calendar.

    Generic on purpose: which calendar, and which refresh token, are constructor arguments.
    Since Phase 24 the registry builds one per product from that product's configuration
    ([D43]), so nothing in here knows or branches on which product it serves.
    """

    name = "google"

    def __init__(
        self,
        *,
        calendar_id: str,
        client_id: str,
        client_secret: str,
        secret_store: SecretStore,
        refresh_token_secret_name: str,
        timezone: str = "UTC",
        timeout_seconds: float = 20.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._calendar_id = calendar_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._secrets = secret_store
        self._refresh_token_secret_name = refresh_token_secret_name
        self._timezone = timezone
        self._timeout = timeout_seconds
        # Injectable so tests supply a mock transport and no request reaches Google.
        self._client = client

        self._refresh_token: str | None = None
        self._access_token: str | None = None
        self._access_token_expires_at: datetime | None = None
        # Serialises refreshes: several concurrent turns would otherwise each mint a
        # token, and one rotation could be lost.
        self._token_lock = asyncio.Lock()

    # ---------------------------------------------------------------- interface

    async def list_busy_intervals(self, window: TimeInterval) -> list[TimeInterval]:
        """What the sales calendar is already occupied by (§12).

        Uses freebusy rather than listing events: it returns only the intervals, so no
        event titles or attendees are read, and a calendar the platform can see but
        should not read in detail stays private.
        """
        body = await self._request(
            "POST",
            f"{CALENDAR_API}/freeBusy",
            json={
                "timeMin": _to_rfc3339(window.start),
                "timeMax": _to_rfc3339(window.end),
                "items": [{"id": self._calendar_id}],
            },
        )

        calendars = body.get("calendars") or {}
        entry = calendars.get(self._calendar_id) or {}

        if entry.get("errors"):
            # A calendar the credentials cannot see reads as "no busy time", which
            # would offer every slot and double-book the team. Refused instead.
            raise NonRetryableProviderError(
                f"Calendar {self._calendar_id!r} is not readable: {entry['errors']}",
                provider=self.name,
            )

        intervals: list[TimeInterval] = []
        for period in entry.get("busy") or []:
            start = _parse_time(period.get("start"))
            end = _parse_time(period.get("end"))
            if start and end and end > start:
                intervals.append(TimeInterval(start=start, end=end))

        return intervals

    async def create_event(self, request: EventRequest) -> CalendarEvent:
        """Create the demo event, converging rather than duplicating on a retry."""
        event_id = _encode_event_id(request.event_id)

        payload: dict[str, Any] = {
            "id": event_id,
            "summary": request.summary,
            "start": {
                "dateTime": _to_rfc3339(request.interval.start),
                "timeZone": request.timezone,
            },
            "end": {
                "dateTime": _to_rfc3339(request.interval.end),
                "timeZone": request.timezone,
            },
        }
        if request.description:
            payload["description"] = request.description
        if request.attendee_emails:
            payload["attendees"] = [{"email": email} for email in request.attendee_emails]
        if request.metadata:
            # Private extended properties: visible to this application, not to anyone
            # reading the event in the calendar UI.
            payload["extendedProperties"] = {"private": dict(request.metadata)}

        try:
            body = await self._request(
                "POST",
                f"{CALENDAR_API}/calendars/{self._calendar_id}/events",
                json=payload,
                # Google emails the attendee itself; the platform sends its own
                # WhatsApp confirmation, so this stays off to avoid two of them.
                params={"sendUpdates": "none"},
            )
        except _AlreadyExistsError:
            # The retry's own earlier success. Fetching it is what makes a redelivered
            # booking task converge on one event (§20).
            existing = await self.get_event(request.event_id)
            if existing is not None:
                logger.info("calendar_event_already_created", event_id=request.event_id)
                return existing
            # Deleted between the conflict and the read, or belongs to another
            # calendar. Either way the slot is not ours to claim.
            raise SlotUnavailableError(
                f"Event {request.event_id} conflicts but cannot be read back"
            ) from None

        return _parse_event(body, fallback_timezone=request.timezone)

    async def get_event(self, event_id: str) -> CalendarEvent | None:
        encoded = _encode_event_id(event_id)
        body = await self._request(
            "GET",
            f"{CALENDAR_API}/calendars/{self._calendar_id}/events/{encoded}",
            allow_missing=True,
        )
        if body is None:
            return None
        if body.get("status") == "cancelled":
            # Google keeps cancelled events readable. Treated as absent, so a
            # cancelled booking does not look confirmed.
            return None
        return _parse_event(body, fallback_timezone=self._timezone)

    async def delete_event(self, event_id: str) -> None:
        """Idempotent: deleting an absent event is not an error."""
        encoded = _encode_event_id(event_id)
        await self._request(
            "DELETE",
            f"{CALENDAR_API}/calendars/{self._calendar_id}/events/{encoded}",
            allow_missing=True,
            params={"sendUpdates": "none"},
        )

    # ---------------------------------------------------------------- auth

    async def _get_access_token(self) -> str:
        """A valid access token, minting one from the refresh token when needed."""
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

    async def _get_refresh_token(self) -> str:
        if self._refresh_token:
            return self._refresh_token

        token = await self._secrets.get_secret(self._refresh_token_secret_name)
        if not token:
            # Nothing to retry: someone must complete the one-time OAuth consent and
            # store the result ([D19]).
            raise NonRetryableProviderError(
                f"No Google refresh token in secret {self._refresh_token_secret_name!r}. "
                "Complete the OAuth consent flow and store the refresh token.",
                provider=self.name,
            )

        self._refresh_token = token
        return token

    async def _exchange_refresh_token(self, refresh_token: str) -> tuple[str, datetime, str | None]:
        from datetime import timedelta

        data = {
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }

        try:
            response = await self._send("POST", TOKEN_URL, data=data)
        except httpx.TimeoutException as exc:
            raise RetryableProviderError(
                f"Token request timed out: {exc}", provider=self.name
            ) from exc
        except httpx.HTTPError as exc:
            raise RetryableProviderError(
                f"Token request failed: {exc}", provider=self.name
            ) from exc

        if not response.is_success:
            detail = _error_detail(response)
            if response.status_code in (400, 401):
                # invalid_grant: the token was revoked or expired. Retrying cannot
                # help; the consent flow must be run again.
                raise NonRetryableProviderError(
                    f"Google rejected the refresh token: {detail}", provider=self.name
                )
            raise RetryableProviderError(
                f"Token exchange failed ({response.status_code}): {detail}",
                provider=self.name,
            )

        body = response.json()
        access_token = body.get("access_token")
        if not access_token:
            raise NonRetryableProviderError("Google returned no access token", provider=self.name)

        expires_in = int(body.get("expires_in") or 3600)
        expires_at = datetime.now(UTC) + timedelta(seconds=max(expires_in - 60, 30))

        return access_token, expires_at, body.get("refresh_token")

    # ---------------------------------------------------------------- transport

    async def _request(
        self,
        method: str,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        allow_missing: bool = False,
    ) -> Any:
        token = await self._get_access_token()
        headers = {"Authorization": f"Bearer {token}"}

        try:
            response = await self._send(method, url, json=json, params=params, headers=headers)
        except httpx.TimeoutException as exc:
            # Says nothing about whether Google processed it. Retryable, and the
            # client-supplied event ID is what stops a double booking (§20).
            raise RetryableProviderError(f"Request timed out: {exc}", provider=self.name) from exc
        except httpx.HTTPError as exc:
            raise RetryableProviderError(f"Network failure: {exc}", provider=self.name) from exc

        if response.status_code == 404 and allow_missing:
            return None
        if response.status_code == 409:
            raise _AlreadyExistsError
        if response.status_code == 410 and allow_missing:
            # Already deleted. Deleting an absent event is not an error.
            return None

        if response.is_success:
            if response.status_code == 204 or not response.content:
                return {}
            try:
                return response.json()
            except ValueError as exc:
                raise NonRetryableProviderError(
                    "Google returned a success status with an unreadable body",
                    provider=self.name,
                ) from exc

        raise self._classify_error(response)

    async def _send(
        self,
        method: str,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
    ) -> httpx.Response:
        if self._client is not None:
            return await self._client.request(
                method,
                url,
                json=json,
                params=params,
                headers=headers,
                data=data,
                timeout=self._timeout,
            )
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await client.request(
                method, url, json=json, params=params, headers=headers, data=data
            )

    def _classify_error(self, response: httpx.Response) -> Exception:
        """The §20 retryable / permanent split."""
        detail = _error_detail(response)
        status = response.status_code

        if status == 429:
            return RetryableProviderError(f"Rate limited: {detail}", provider=self.name)
        if status in (401, 403):
            # 403 is also Google's rate-limit answer, distinguished by reason.
            if "rateLimitExceeded" in detail or "userRateLimitExceeded" in detail:
                return RetryableProviderError(f"Rate limited: {detail}", provider=self.name)
            return NonRetryableProviderError(
                f"Calendar access denied ({status}): {detail}", provider=self.name
            )
        if status >= 500:
            return RetryableProviderError(
                f"Google server error {status}: {detail}", provider=self.name
            )
        if 400 <= status < 500:
            return NonRetryableProviderError(f"[{status}] {detail}", provider=self.name)

        return RetryableProviderError(f"Unexpected status {status}: {detail}", provider=self.name)


class _AlreadyExistsError(Exception):
    """Google reported the event ID as taken (409)."""


# ---------------------------------------------------------------- helpers


def _encode_event_id(event_id: str) -> str:
    """Turn an application event ID into one Google accepts.

    Google permits only base32hex characters, so the hyphens and uppercase in an ID
    like `demo-<uuid>-<timestamp>` are not allowed. Hex encoding the UTF-8 bytes uses
    only `0-9a-f`, which is inside that alphabet.

    Hex rather than a substitution table because it is **injective**: two different
    application IDs can never produce the same Google ID, so one booking can never
    collide with another's event. It is also deterministic, which is what the whole
    idempotency story rests on — the same booking must produce the same Google ID on
    every attempt, in every release.
    """
    encoded = event_id.encode("utf-8").hex()
    # Google requires 5-1024 characters.
    return encoded.ljust(5, "0")[:1024]


def _decode_event_id(encoded: str) -> str | None:
    """Recover the application ID, or None when this event did not come from us."""
    try:
        return bytes.fromhex(encoded).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def _to_rfc3339(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise ValueError("Calendar times must be timezone-aware")
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _parse_event(body: dict[str, Any], *, fallback_timezone: str) -> CalendarEvent:
    start = _parse_time((body.get("start") or {}).get("dateTime"))
    end = _parse_time((body.get("end") or {}).get("dateTime"))

    if start is None or end is None or end <= start:
        raise NonRetryableProviderError(
            "Google returned an event without a usable time range", provider="google"
        )

    attendees = tuple(
        attendee["email"] for attendee in (body.get("attendees") or []) if attendee.get("email")
    )

    google_id = body.get("id", "")

    return CalendarEvent(
        # Decoded back into the application's own ID space, which is what the booking
        # row stores and what a retry looks the event up by. An event created by a
        # human in the calendar UI will not decode, and keeps its Google ID.
        event_id=_decode_event_id(google_id) or google_id,
        interval=TimeInterval(start=start, end=end),
        timezone=(body.get("start") or {}).get("timeZone") or fallback_timezone,
        summary=body.get("summary") or "",
        attendee_emails=attendees,
        meeting_link=body.get("hangoutLink") or body.get("htmlLink"),
    )


def _error_detail(response: httpx.Response) -> str:
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


__all__ = ["GoogleCalendarProvider"]
