"""Google Calendar provider (specification §12, §20, [D7], [D19], [D62]).

The real implementation behind `CalendarProvider`. One shared sales calendar ([D7]);
per-salesperson calendars are not V1.

Spoken to over plain HTTPS rather than through `google-api-python-client`. Two reasons:
the client library is synchronous and would need a thread for every call, and a mock
transport is the only way to test this without a real calendar. The API surface used
here is three endpoints.

**Authentication is not this module's business.** It asks a `CalendarAuthenticator` for
an access token before each call (`app.integrations.calendar.google_auth`), which is
where both the Secret Manager refresh token ([D19]) and the service-account identity
([D62]) live. The provider therefore holds no credential, no expiry arithmetic and no
lock: swapping how the platform proves who it is does not touch a line of the calendar
logic.

Idempotency (§12, §20) rests on Google accepting a client-supplied event ID. A retried
booking sends the same ID; Google answers 409, and the provider fetches the existing
event and returns it. A retry therefore converges on one event rather than
double-booking the sales team.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.errors import NonRetryableProviderError, RetryableProviderError
from app.core.logging import get_logger
from app.integrations.calendar.google_auth import CalendarAuthenticator, error_detail
from app.integrations.calendar.provider import (
    CalendarEvent,
    CalendarProvider,
    EventRequest,
    SlotUnavailableError,
    TimeInterval,
)

logger = get_logger(__name__)

CALENDAR_API = "https://www.googleapis.com/calendar/v3"

#: Google's refusal to attach a conference to an event on a calendar that cannot host
#: one: ``400 Invalid conference type value``. Matched on the word rather than the whole
#: sentence, because Google words it differently for other conference types and the
#: meaning — "this calendar refuses conferences" — is what the fallback acts on.
_CONFERENCE_REJECTED = re.compile(r"conference", re.IGNORECASE)


class GoogleCalendarProvider(CalendarProvider):
    """Reads availability from and writes events to one Google calendar.

    Generic on purpose: which calendar is a constructor argument, and how the platform
    authenticates arrives as `auth`. Since Phase 24 the registry builds one per product
    from that product's configuration ([D43]), so nothing in here knows or branches on
    which product it serves — or on how identity is proven.
    """

    name = "google"

    def __init__(
        self,
        *,
        calendar_id: str,
        auth: CalendarAuthenticator,
        timezone: str = "UTC",
        timeout_seconds: float = 20.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._calendar_id = calendar_id
        self._auth = auth
        self._timezone = timezone
        self._timeout = timeout_seconds
        # Injectable so tests supply a mock transport and no request reaches Google.
        self._client = client

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
        if request.attendee_emails and self._auth.supports_attendees:
            payload["attendees"] = [{"email": email} for email in request.attendee_emails]
        elif request.attendee_emails:
            # A service account without Domain-Wide Delegation gets a 403 for any
            # event carrying `attendees` (`forbiddenForServiceAccounts`), which
            # would fail every booking for a lead with an email. The invite is
            # dropped — never attempted — while the contact address stays in the
            # description, where the sales team still sees it.
            logger.info(
                "calendar_attendees_omitted",
                calendar_id=self._calendar_id,
                attendee_count=len(request.attendee_emails),
            )
        if request.metadata:
            # Private extended properties: visible to this application, not to anyone
            # reading the event in the calendar UI.
            payload["extendedProperties"] = {"private": dict(request.metadata)}
        if request.create_meeting:
            # Stable requestId: a retried create for the same booking must not ask
            # Google for a second Meet conference on a different id.
            payload["conferenceData"] = {
                "createRequest": {
                    "requestId": event_id,
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                }
            }

        # Google emails the attendee itself; the platform sends its own WhatsApp
        # confirmation, so sendUpdates stays off to avoid two of them.
        params: dict[str, str] = {"sendUpdates": "none"}
        if request.create_meeting:
            # Without conferenceDataVersion, Google silently ignores conferenceData.
            params["conferenceDataVersion"] = "1"

        try:
            body = await self._post_event(
                f"{CALENDAR_API}/calendars/{self._calendar_id}/events",
                payload=payload,
                params=params,
                wants_meeting=request.create_meeting,
            )
        except _AlreadyExistsError:
            # The retry's own earlier success. Fetching it is what makes a redelivered
            # booking task converge on one event (§20).
            existing = await self.get_event(request.event_id)
            if existing is not None:
                logger.info("calendar_event_already_created", event_id=request.event_id)
                self._note_missing_meeting(existing, wants_meeting=request.create_meeting)
                return existing
            # Deleted between the conflict and the read, or belongs to another
            # calendar. Either way the slot is not ours to claim.
            raise SlotUnavailableError(
                f"Event {request.event_id} conflicts but cannot be read back"
            ) from None

        event = _parse_event(body, fallback_timezone=request.timezone)
        self._note_missing_meeting(event, wants_meeting=request.create_meeting)
        return event

    async def _post_event(
        self,
        url: str,
        *,
        payload: dict[str, Any],
        params: dict[str, str],
        wants_meeting: bool,
    ) -> Any:
        """Create the event, dropping the Meet request if this calendar refuses one.

        Not every calendar can host a conference. A secondary calendar in a personal
        Google account has no conference solutions at all, and Google answers the
        insert with ``400 Invalid conference type value`` — on staging 2026-09-27 that
        failed *every* booking, because a booking without a join link was treated as
        no booking. It is still a booking: the customer has a time, the team has a
        diary entry, and the join link is the team's to send. So the request is made
        once more without the conference, and the missing link is reported through
        `calendar_meet_unavailable` rather than raised at the customer.
        """
        try:
            return await self._request("POST", url, json=payload, params=params)
        except NonRetryableProviderError as exc:
            if not (wants_meeting and _CONFERENCE_REJECTED.search(str(exc))):
                raise
            logger.warning(
                "calendar_meet_unavailable",
                calendar_id=self._calendar_id,
                detail=str(exc),
            )
            # conferenceDataVersion is only meaningful alongside conferenceData; both
            # go, so the retry is an ordinary time-only event.
            return await self._request(
                "POST",
                url,
                json={key: value for key, value in payload.items() if key != "conferenceData"},
                params={
                    key: value for key, value in params.items() if key != "conferenceDataVersion"
                },
            )

    def _note_missing_meeting(self, event: CalendarEvent, *, wants_meeting: bool) -> None:
        """Record that a booking went through without a join link, and carry on.

        A meeting that was asked for and not returned is worth seeing — it is how a
        calendar that *could* host Meet would announce it had stopped — but it is not
        worth failing a confirmed booking over.
        """
        if wants_meeting and not event.meeting_link:
            logger.warning(
                "calendar_meet_missing",
                calendar_id=self._calendar_id,
                event_id=event.event_id,
            )

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
        """A valid access token, from whichever authenticator this provider was given.

        Renewal, rotation and expiry are the authenticator's concern; the provider only
        needs a token that works.
        """
        return await self._auth.access_token()

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
        detail = error_detail(response)
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


def _meeting_link_from_event(body: dict[str, Any]) -> str | None:
    """A real Meet join URL, never the calendar event page (`htmlLink`)."""
    hangout = body.get("hangoutLink")
    if isinstance(hangout, str) and hangout.strip():
        return hangout.strip()

    conference = body.get("conferenceData") or {}
    for entry in conference.get("entryPoints") or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("entryPointType") != "video":
            continue
        uri = entry.get("uri")
        if isinstance(uri, str) and uri.strip():
            return uri.strip()
    return None


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
        meeting_link=_meeting_link_from_event(body),
    )


__all__ = ["GoogleCalendarProvider"]
