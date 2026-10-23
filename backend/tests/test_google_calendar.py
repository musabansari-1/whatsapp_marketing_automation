"""Google Calendar provider tests (§12, §19, §20, [D7], [D19]).

Every request is served by an in-memory mock transport: no socket is opened, no real
calendar is touched, and no Google credential exists.

The three things worth getting wrong here are the token lifecycle (including rotation,
which fails silently months later if missed), the event-ID idempotency that stops a
retry double-booking, and the retryable/permanent split.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from app.core.errors import NonRetryableProviderError, RetryableProviderError
from app.integrations.calendar import EventRequest, SlotUnavailableError, TimeInterval
from app.integrations.calendar.google import (
    GoogleCalendarProvider,
    _decode_event_id,
    _encode_event_id,
)
from app.integrations.calendar.google_auth import (
    CALENDAR_SCOPE,
    OAuthRefreshTokenAuthenticator,
    ServiceAccountAuthenticator,
)
from app.integrations.secrets import InMemorySecretStore

CALENDAR_ID = "sales@example.com"
SECRET_NAME = "google-calendar-refresh-token"  # noqa: S105 - a name, not a secret
START = datetime(2026, 9, 2, 10, 0, tzinfo=UTC)


def make_provider(
    handler: Any, *, store: InMemorySecretStore | None = None
) -> tuple[GoogleCalendarProvider, InMemorySecretStore]:
    """A provider authenticating with a refresh token, over one mock transport.

    The same `httpx.AsyncClient` serves the token endpoint and the calendar calls, which
    is what lets `routed()` answer both.
    """
    secret_store = store or InMemorySecretStore({SECRET_NAME: "refresh-token-1"})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = GoogleCalendarProvider(
        calendar_id=CALENDAR_ID,
        auth=OAuthRefreshTokenAuthenticator(
            client_id="client-id",
            client_secret="client-secret",  # noqa: S106 - never leaves the mock
            secret_store=secret_store,
            secret_name=SECRET_NAME,
            client=client,
        ),
        timezone="Asia/Kolkata",
        client=client,
    )
    return provider, secret_store


def make_service_account_provider(
    handler: Any,
    *,
    credentials: Any,
) -> tuple[GoogleCalendarProvider, Any]:
    """A provider authenticating as a service account, over one mock transport.

    The credentials are injected, so the whole lifecycle is exercised without a GCP
    identity, a key file or a socket: nothing here resolves Application Default
    Credentials.
    """
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    auth = ServiceAccountAuthenticator(credentials=credentials, request=object())
    provider = GoogleCalendarProvider(
        calendar_id=CALENDAR_ID,
        auth=auth,
        timezone="Asia/Kolkata",
        client=client,
    )
    return provider, auth


class FakeServiceAccountCredentials:
    """A stand-in for `google.auth` credentials ([D62]).

    Records how often it was asked to refresh, so a test can prove the token is cached
    rather than re-minted per call, and can be made to fail the way Google fails.
    """

    def __init__(
        self,
        *,
        token: str | None = "service-account-token",  # noqa: S106, S107 - a mock, never a secret
        expires_in: float = 3600.0,
        raises: Exception | None = None,
    ) -> None:
        self.token = token
        self.expiry: datetime | None = datetime.now(UTC) + timedelta(seconds=expires_in)
        self.raises = raises
        self.refreshes = 0
        self.requested_with: list[Any] = []

    def refresh(self, request: Any) -> None:
        self.refreshes += 1
        self.requested_with.append(request)
        if self.raises is not None:
            raise self.raises


def token_response(**extra: Any) -> dict[str, Any]:
    return {"access_token": "access-1", "expires_in": 3600, **extra}


def event_body(event_id: str = "abc", **extra: Any) -> dict[str, Any]:
    body = {
        "id": event_id,
        "summary": "Demo",
        "start": {"dateTime": "2026-09-02T10:00:00Z", "timeZone": "Asia/Kolkata"},
        "end": {"dateTime": "2026-09-02T10:30:00Z", "timeZone": "Asia/Kolkata"},
        "htmlLink": "https://calendar.google.com/event?eid=abc",
        "hangoutLink": "https://meet.google.com/abc-defg-hij",
    }
    body.update(extra)
    return body


def routed(
    *,
    token: dict[str, Any] | None = None,
    freebusy: dict[str, Any] | None = None,
    event: dict[str, Any] | None = None,
    event_status: int = 200,
    token_status: int = 200,
) -> Any:
    """A handler that answers the token endpoint and one calendar endpoint."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "oauth2.googleapis.com" in str(request.url):
            return httpx.Response(token_status, json=token or token_response())
        if "freeBusy" in str(request.url):
            return httpx.Response(200, json=freebusy or {"calendars": {CALENDAR_ID: {"busy": []}}})
        return httpx.Response(event_status, json=event or event_body())

    return handler


def an_event_request(event_id: str = "demo-abc-123") -> EventRequest:
    return EventRequest(
        event_id=event_id,
        interval=TimeInterval(start=START, end=START + timedelta(minutes=30)),
        timezone="Asia/Kolkata",
        summary="Demo — Asha",
        metadata={"lead_id": "lead-1"},
    )


class TestEventIdEncoding:
    def test_encoding_round_trips(self) -> None:
        """The booking row stores the application's ID, and a retry looks the event up
        by it — so the mapping must be reversible."""
        original = "demo-4f2c1e9a-1756800000"
        assert _decode_event_id(_encode_event_id(original)) == original

    def test_encoding_is_deterministic(self) -> None:
        """The whole idempotency story rests on this: the same booking must produce
        the same Google ID on every attempt, in every release."""
        assert _encode_event_id("demo-1") == _encode_event_id("demo-1")

    def test_different_ids_never_collide(self) -> None:
        """Hex is injective, so one booking can never claim another's event."""
        ids = {_encode_event_id(f"demo-{n}") for n in range(500)}
        assert len(ids) == 500

    def test_the_result_is_acceptable_to_google(self) -> None:
        """Google permits only base32hex characters, 5-1024 of them."""
        encoded = _encode_event_id("demo-ABC-123")
        assert set(encoded) <= set("0123456789abcdefghijklmnopqrstuv")
        assert 5 <= len(encoded) <= 1024

    def test_a_short_id_is_padded(self) -> None:
        assert len(_encode_event_id("a")) >= 5

    def test_a_foreign_id_does_not_decode(self) -> None:
        """An event a human created in the calendar UI keeps its Google ID."""
        assert _decode_event_id("someGoogleId") is None


class TestAuthentication:
    @pytest.mark.asyncio
    async def test_the_refresh_token_comes_from_the_secret_store(self) -> None:
        """[D19]: never from configuration, never from the database."""
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if "oauth2" in str(request.url):
                seen["body"] = request.content.decode()
                return httpx.Response(200, json=token_response())
            return httpx.Response(200, json={"calendars": {CALENDAR_ID: {"busy": []}}})

        provider, _ = make_provider(handler)
        await provider.list_busy_intervals(TimeInterval(start=START, end=START + timedelta(days=1)))

        assert "refresh-token-1" in seen["body"]

    @pytest.mark.asyncio
    async def test_a_missing_refresh_token_is_a_permanent_failure(self) -> None:
        """Someone must run the one-time OAuth consent; no retry produces a token."""
        provider, _ = make_provider(routed(), store=InMemorySecretStore())

        with pytest.raises(NonRetryableProviderError, match="OAuth consent"):
            await provider.list_busy_intervals(
                TimeInterval(start=START, end=START + timedelta(days=1))
            )

    @pytest.mark.asyncio
    async def test_a_rotated_refresh_token_is_written_back(self) -> None:
        """The failure that shows up months later: Google rotates the token, the
        worker keeps using the old one, and everything works until it expires."""
        provider, store = make_provider(
            routed(token=token_response(refresh_token="refresh-token-2"))
        )

        await provider.list_busy_intervals(TimeInterval(start=START, end=START + timedelta(days=1)))

        assert store.values[SECRET_NAME] == "refresh-token-2"
        assert store.writes == [SECRET_NAME]

    @pytest.mark.asyncio
    async def test_an_unrotated_token_is_not_rewritten(self) -> None:
        """A new secret version per booking would be noise, and Secret Manager
        versions are not free."""
        provider, store = make_provider(
            routed(token=token_response(refresh_token="refresh-token-1"))
        )

        await provider.list_busy_intervals(TimeInterval(start=START, end=START + timedelta(days=1)))

        assert store.writes == []

    @pytest.mark.asyncio
    async def test_the_access_token_is_reused(self) -> None:
        """One token exchange per process, not one per call."""
        calls = {"token": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if "oauth2" in str(request.url):
                calls["token"] += 1
                return httpx.Response(200, json=token_response())
            return httpx.Response(200, json={"calendars": {CALENDAR_ID: {"busy": []}}})

        provider, _ = make_provider(handler)
        window = TimeInterval(start=START, end=START + timedelta(days=1))

        await provider.list_busy_intervals(window)
        await provider.list_busy_intervals(window)

        assert calls["token"] == 1

    @pytest.mark.asyncio
    async def test_a_rejected_refresh_token_is_permanent(self) -> None:
        """invalid_grant means revoked or expired; the consent flow must be rerun."""
        provider, _ = make_provider(routed(token={"error": "invalid_grant"}, token_status=400))

        with pytest.raises(NonRetryableProviderError, match="rejected the refresh token"):
            await provider.list_busy_intervals(
                TimeInterval(start=START, end=START + timedelta(days=1))
            )

    @pytest.mark.asyncio
    async def test_a_token_server_error_is_retryable(self) -> None:
        provider, _ = make_provider(routed(token={"error": "backend_error"}, token_status=503))

        with pytest.raises(RetryableProviderError):
            await provider.list_busy_intervals(
                TimeInterval(start=START, end=START + timedelta(days=1))
            )

    @pytest.mark.asyncio
    async def test_the_client_secret_never_appears_in_an_error(self) -> None:
        """§19: an error message ends up in the log store."""
        provider, _ = make_provider(routed(token={"error": "invalid_client"}, token_status=401))

        with pytest.raises(NonRetryableProviderError) as caught:
            await provider.list_busy_intervals(
                TimeInterval(start=START, end=START + timedelta(days=1))
            )

        assert "client-secret" not in str(caught.value)


class TestServiceAccountAuthentication:
    """[D62]: the production path.

    No refresh token exists, so nothing expires after seven days, nothing is written back
    to Secret Manager, and no human consent stands between the platform and a booking.
    """

    @pytest.mark.asyncio
    async def test_the_service_accounts_token_authenticates_calendar_calls(self) -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["authorization"] = request.headers.get("Authorization")
            return httpx.Response(200, json={"calendars": {CALENDAR_ID: {"busy": []}}})

        provider, _ = make_service_account_provider(
            handler, credentials=FakeServiceAccountCredentials()
        )

        await provider.list_busy_intervals(TimeInterval(start=START, end=START + timedelta(days=1)))

        assert seen["authorization"] == "Bearer service-account-token"

    @pytest.mark.asyncio
    async def test_the_token_is_minted_once_and_reused(self) -> None:
        """One refresh per process, not one per call."""
        credentials = FakeServiceAccountCredentials()
        provider, _ = make_service_account_provider(routed(), credentials=credentials)
        window = TimeInterval(start=START, end=START + timedelta(days=1))

        await provider.list_busy_intervals(window)
        await provider.list_busy_intervals(window)

        assert credentials.refreshes == 1

    @pytest.mark.asyncio
    async def test_an_expired_token_is_minted_again(self) -> None:
        """The cached token is not trusted past its expiry."""
        credentials = FakeServiceAccountCredentials()
        provider, auth = make_service_account_provider(routed(), credentials=credentials)
        window = TimeInterval(start=START, end=START + timedelta(days=1))

        await provider.list_busy_intervals(window)
        auth._expires_at = datetime.now(UTC) - timedelta(seconds=1)  # noqa: SLF001
        await provider.list_busy_intervals(window)

        assert credentials.refreshes == 2

    @pytest.mark.asyncio
    async def test_the_calendar_scope_is_requested(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Read and write: the same scope the OAuth path asks a human for, so both
        arrangements see one calendar."""
        seen: dict[str, Any] = {}
        credentials = FakeServiceAccountCredentials()

        def default(*, scopes: list[str]) -> tuple[Any, str]:
            seen["scopes"] = scopes
            return credentials, "project"

        monkeypatch.setattr("google.auth.default", default)
        provider, _ = make_service_account_provider(routed(), credentials=None)

        await provider.list_busy_intervals(TimeInterval(start=START, end=START + timedelta(days=1)))

        assert seen["scopes"] == [CALENDAR_SCOPE]

    @pytest.mark.asyncio
    async def test_missing_default_credentials_are_permanent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """What an operator meets when nothing has granted the identity a calendar."""
        from google.auth.exceptions import DefaultCredentialsError

        def no_credentials(*_: Any, **__: Any) -> Any:
            raise DefaultCredentialsError("no credentials")  # type: ignore[no-untyped-call]

        monkeypatch.setattr("google.auth.default", no_credentials)
        provider, _ = make_service_account_provider(routed(), credentials=None)

        with pytest.raises(NonRetryableProviderError, match="application-default login"):
            await provider.list_busy_intervals(
                TimeInterval(start=START, end=START + timedelta(days=1))
            )

    @pytest.mark.asyncio
    async def test_a_rejected_credential_is_permanent(self) -> None:
        """A disabled or deleted service account. Retrying with the same identity fails
        identically, so the turn must not burn its retry budget on it."""
        from google.auth.exceptions import RefreshError

        credentials = FakeServiceAccountCredentials(raises=RefreshError("invalid_grant"))  # type: ignore[no-untyped-call]
        provider, _ = make_service_account_provider(routed(), credentials=credentials)

        with pytest.raises(NonRetryableProviderError, match="service account"):
            await provider.list_busy_intervals(
                TimeInterval(start=START, end=START + timedelta(days=1))
            )

    @pytest.mark.asyncio
    async def test_a_transport_failure_while_minting_is_retryable(self) -> None:
        """The metadata server being briefly unreachable is worth another attempt."""
        from google.auth.exceptions import TransportError

        credentials = FakeServiceAccountCredentials(raises=TransportError("network down"))  # type: ignore[no-untyped-call]
        provider, _ = make_service_account_provider(routed(), credentials=credentials)

        with pytest.raises(RetryableProviderError, match="Could not reach Google"):
            await provider.list_busy_intervals(
                TimeInterval(start=START, end=START + timedelta(days=1))
            )

    @pytest.mark.asyncio
    async def test_a_token_without_a_value_is_permanent(self) -> None:
        credentials = FakeServiceAccountCredentials(token=None)
        provider, _ = make_service_account_provider(routed(), credentials=credentials)

        with pytest.raises(NonRetryableProviderError, match="no access token"):
            await provider.list_busy_intervals(
                TimeInterval(start=START, end=START + timedelta(days=1))
            )


class TestAvailability:
    @pytest.mark.asyncio
    async def test_busy_intervals_are_returned(self) -> None:
        busy = {
            "calendars": {
                CALENDAR_ID: {
                    "busy": [{"start": "2026-09-02T11:00:00Z", "end": "2026-09-02T12:00:00Z"}]
                }
            }
        }
        provider, _ = make_provider(routed(freebusy=busy))

        intervals = await provider.list_busy_intervals(
            TimeInterval(start=START, end=START + timedelta(days=1))
        )

        assert len(intervals) == 1
        assert intervals[0].start == datetime(2026, 9, 2, 11, 0, tzinfo=UTC)

    @pytest.mark.asyncio
    async def test_an_empty_calendar_returns_nothing(self) -> None:
        provider, _ = make_provider(routed())

        assert (
            await provider.list_busy_intervals(
                TimeInterval(start=START, end=START + timedelta(days=1))
            )
            == []
        )

    @pytest.mark.asyncio
    async def test_an_unreadable_calendar_is_refused_not_treated_as_free(self) -> None:
        """The dangerous default: a calendar the credentials cannot see would read as
        'no busy time', offer every slot, and double-book the team."""
        broken = {"calendars": {CALENDAR_ID: {"errors": [{"reason": "notFound"}]}}}
        provider, _ = make_provider(routed(freebusy=broken))

        with pytest.raises(NonRetryableProviderError, match="not readable"):
            await provider.list_busy_intervals(
                TimeInterval(start=START, end=START + timedelta(days=1))
            )

    @pytest.mark.asyncio
    async def test_a_malformed_interval_is_skipped(self) -> None:
        messy = {
            "calendars": {
                CALENDAR_ID: {
                    "busy": [
                        {"start": "not-a-time", "end": "2026-09-02T12:00:00Z"},
                        {"start": "2026-09-02T13:00:00Z", "end": "2026-09-02T14:00:00Z"},
                    ]
                }
            }
        }
        provider, _ = make_provider(routed(freebusy=messy))

        intervals = await provider.list_busy_intervals(
            TimeInterval(start=START, end=START + timedelta(days=1))
        )
        assert len(intervals) == 1


class TestEventCreation:
    @pytest.mark.asyncio
    async def test_an_event_is_created(self) -> None:
        encoded = _encode_event_id("demo-abc-123")
        provider, _ = make_provider(routed(event=event_body(event_id=encoded)))

        event = await provider.create_event(an_event_request())

        assert event.event_id == "demo-abc-123"
        assert event.interval.start == START

    @pytest.mark.asyncio
    async def test_the_client_supplied_id_is_sent(self) -> None:
        """Google accepting our ID is what makes the retry converge."""
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if "oauth2" in str(request.url):
                return httpx.Response(200, json=token_response())
            import json

            seen.update(json.loads(request.content))
            return httpx.Response(200, json=event_body(event_id=seen["id"]))

        provider, _ = make_provider(handler)
        await provider.create_event(an_event_request())

        assert seen["id"] == _encode_event_id("demo-abc-123")

    @pytest.mark.asyncio
    async def test_a_conflict_converges_on_the_existing_event(self) -> None:
        """§20: a redelivered booking task must not create a second event."""
        encoded = _encode_event_id("demo-abc-123")
        state = {"created": False}

        def handler(request: httpx.Request) -> httpx.Response:
            if "oauth2" in str(request.url):
                return httpx.Response(200, json=token_response())
            if request.method == "POST":
                state["created"] = True
                return httpx.Response(409, json={"error": {"message": "duplicate"}})
            return httpx.Response(200, json=event_body(event_id=encoded))

        provider, _ = make_provider(handler)
        event = await provider.create_event(an_event_request())

        assert state["created"] is True
        assert event.event_id == "demo-abc-123"

    @pytest.mark.asyncio
    async def test_a_conflict_that_cannot_be_read_back_is_unavailable(self) -> None:
        """Deleted between the conflict and the read, or another calendar's. Either
        way the slot is not ours to claim."""

        def handler(request: httpx.Request) -> httpx.Response:
            if "oauth2" in str(request.url):
                return httpx.Response(200, json=token_response())
            if request.method == "POST":
                return httpx.Response(409, json={"error": {"message": "duplicate"}})
            return httpx.Response(404, json={"error": {"message": "gone"}})

        provider, _ = make_provider(handler)

        with pytest.raises(SlotUnavailableError):
            await provider.create_event(an_event_request())

    @pytest.mark.asyncio
    async def test_attendees_and_metadata_are_sent(self) -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if "oauth2" in str(request.url):
                return httpx.Response(200, json=token_response())
            import json

            seen.update(json.loads(request.content))
            return httpx.Response(200, json=event_body(event_id=seen["id"]))

        provider, _ = make_provider(handler)
        request = EventRequest(
            event_id="demo-1",
            interval=TimeInterval(start=START, end=START + timedelta(minutes=30)),
            timezone="Asia/Kolkata",
            summary="Demo",
            attendee_emails=("asha@example.com",),
            metadata={"lead_id": "lead-1"},
        )
        await provider.create_event(request)

        assert seen["attendees"] == [{"email": "asha@example.com"}]
        assert seen["extendedProperties"]["private"]["lead_id"] == "lead-1"

    @pytest.mark.asyncio
    async def test_google_does_not_email_the_attendee(self) -> None:
        """The platform sends its own WhatsApp confirmation; two would be confusing."""
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if "oauth2" in str(request.url):
                return httpx.Response(200, json=token_response())
            seen["params"] = dict(request.url.params)
            return httpx.Response(200, json=event_body())

        provider, _ = make_provider(handler)
        await provider.create_event(an_event_request())

        assert seen["params"]["sendUpdates"] == "none"

    @pytest.mark.asyncio
    async def test_a_meet_conference_is_requested(self) -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if "oauth2" in str(request.url):
                return httpx.Response(200, json=token_response())
            import json

            seen["params"] = dict(request.url.params)
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json=event_body(event_id=seen["body"]["id"]))

        provider, _ = make_provider(handler)
        event = await provider.create_event(an_event_request())

        assert seen["params"]["conferenceDataVersion"] == "1"
        assert seen["params"]["sendUpdates"] == "none"
        create_request = seen["body"]["conferenceData"]["createRequest"]
        assert create_request["conferenceSolutionKey"] == {"type": "hangoutsMeet"}
        assert create_request["requestId"] == _encode_event_id("demo-abc-123")
        assert event.meeting_link == "https://meet.google.com/abc-defg-hij"

    @pytest.mark.asyncio
    async def test_a_calendar_that_refuses_meet_still_books(self) -> None:
        """The staging 2026-09-27 failure: Google answers `400 Invalid conference type
        value` on a calendar with no conference solutions, and every booking failed.

        Meet is asked for once and dropped if refused — the booking is what the customer
        is waiting for, and the join link is the team's to send. A regression here
        returns the "couldn't be checked" reply to every customer again.
        """
        attempts: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            if "oauth2" in str(request.url):
                return httpx.Response(200, json=token_response())
            body = json.loads(request.content)
            attempts.append(body)
            if "conferenceData" in body:
                return httpx.Response(
                    400,
                    json={"error": {"message": "Invalid conference type value. invalid"}},
                )
            return httpx.Response(200, json=event_body(event_id=body["id"], hangoutLink=None))

        provider, _ = make_provider(handler)
        event = await provider.create_event(an_event_request())

        assert len(attempts) == 2
        assert "conferenceData" in attempts[0]
        assert "conferenceData" not in attempts[1]
        # The event is real — it is only the join link that is absent.
        assert event.event_id == "demo-abc-123"
        assert event.meeting_link is None

    @pytest.mark.asyncio
    async def test_a_rejected_meet_retry_does_not_send_conference_data_version(self) -> None:
        seen_params: list[dict[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            if "oauth2" in str(request.url):
                return httpx.Response(200, json=token_response())
            seen_params.append(dict(request.url.params))
            body = json.loads(request.content)
            if "conferenceData" in body:
                return httpx.Response(
                    400, json={"error": {"message": "Invalid conference type value. invalid"}}
                )
            return httpx.Response(200, json=event_body(event_id=body["id"], hangoutLink=None))

        provider, _ = make_provider(handler)
        await provider.create_event(an_event_request())

        assert "conferenceDataVersion" in seen_params[0]
        assert "conferenceDataVersion" not in seen_params[1]

    @pytest.mark.asyncio
    async def test_an_unrelated_400_is_not_retried(self) -> None:
        """Only a conference refusal earns the second attempt: retrying a genuine
        validation error would book against a request Google just rejected."""
        attempts = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if "oauth2" in str(request.url):
                return httpx.Response(200, json=token_response())
            attempts["count"] += 1
            return httpx.Response(400, json={"error": {"message": "Invalid start time"}})

        provider, _ = make_provider(handler)

        with pytest.raises(NonRetryableProviderError, match="Invalid start time"):
            await provider.create_event(an_event_request())

        assert attempts["count"] == 1

    @pytest.mark.asyncio
    async def test_a_meeting_less_success_is_not_a_failure(self) -> None:
        """A calendar that simply does not return a link books normally — it is
        recorded, not raised."""
        provider, _ = make_provider(
            routed(event=event_body(hangoutLink=None, conferenceData={"entryPoints": []}))
        )

        event = await provider.create_event(an_event_request())

        assert event.meeting_link is None

    @pytest.mark.asyncio
    async def test_video_entrypoint_is_used_when_hangout_link_absent(self) -> None:
        encoded = _encode_event_id("demo-abc-123")
        provider, _ = make_provider(
            routed(
                event=event_body(
                    event_id=encoded,
                    hangoutLink=None,
                    conferenceData={
                        "entryPoints": [
                            {"entryPointType": "phone", "uri": "tel:+123"},
                            {
                                "entryPointType": "video",
                                "uri": "https://meet.google.com/from-entry",
                            },
                        ]
                    },
                )
            )
        )

        event = await provider.create_event(an_event_request())
        assert event.meeting_link == "https://meet.google.com/from-entry"

    @pytest.mark.asyncio
    async def test_meet_can_be_skipped(self) -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if "oauth2" in str(request.url):
                return httpx.Response(200, json=token_response())
            import json

            seen["params"] = dict(request.url.params)
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json=event_body(event_id=seen["body"]["id"], hangoutLink=None),
            )

        provider, _ = make_provider(handler)
        request = EventRequest(
            event_id="demo-abc-123",
            interval=TimeInterval(start=START, end=START + timedelta(minutes=30)),
            timezone="Asia/Kolkata",
            summary="Demo",
            create_meeting=False,
        )
        event = await provider.create_event(request)

        assert "conferenceDataVersion" not in seen["params"]
        assert "conferenceData" not in seen["body"]
        assert event.meeting_link is None


class TestEventReading:
    @pytest.mark.asyncio
    async def test_a_missing_event_is_none(self) -> None:
        """Lets a retry detect that its own prior attempt did not succeed."""

        def handler(request: httpx.Request) -> httpx.Response:
            if "oauth2" in str(request.url):
                return httpx.Response(200, json=token_response())
            return httpx.Response(404, json={"error": {"message": "not found"}})

        provider, _ = make_provider(handler)
        assert await provider.get_event("demo-abc-123") is None

    @pytest.mark.asyncio
    async def test_a_cancelled_event_reads_as_absent(self) -> None:
        """Google keeps cancelled events readable; treating one as present would make
        a cancelled booking look confirmed."""
        provider, _ = make_provider(routed(event=event_body(status="cancelled")))

        assert await provider.get_event("demo-abc-123") is None

    @pytest.mark.asyncio
    async def test_deleting_an_absent_event_is_not_an_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if "oauth2" in str(request.url):
                return httpx.Response(200, json=token_response())
            return httpx.Response(410, json={"error": {"message": "already deleted"}})

        provider, _ = make_provider(handler)
        await provider.delete_event("demo-abc-123")  # must not raise


class TestFailureClassification:
    @pytest.mark.asyncio
    async def test_rate_limiting_is_retryable(self) -> None:
        provider, _ = make_provider(
            routed(event={"error": {"message": "slow down"}}, event_status=429)
        )

        with pytest.raises(RetryableProviderError, match="Rate limited"):
            await provider.create_event(an_event_request())

    @pytest.mark.asyncio
    async def test_a_403_rate_limit_is_retryable_not_permanent(self) -> None:
        """Google answers rate limits with 403 as well as 429, distinguished only by
        the reason — reading the status alone would abandon a recoverable booking."""
        body = {"error": {"message": "quota", "errors": [{"reason": "rateLimitExceeded"}]}}
        provider, _ = make_provider(routed(event=body, event_status=403))

        with pytest.raises(RetryableProviderError, match="Rate limited"):
            await provider.create_event(an_event_request())

    @pytest.mark.asyncio
    async def test_a_genuine_403_is_permanent(self) -> None:
        body = {"error": {"message": "forbidden", "errors": [{"reason": "forbidden"}]}}
        provider, _ = make_provider(routed(event=body, event_status=403))

        with pytest.raises(NonRetryableProviderError, match="access denied"):
            await provider.create_event(an_event_request())

    @pytest.mark.asyncio
    async def test_a_service_account_never_sends_attendees_but_keeps_everything_else(
        self,
    ) -> None:
        """The production 403 ([D62], staging 2026-09-27): a service account without
        Domain-Wide Delegation cannot invite anyone, and every event we send on a
        lead with an email carries an attendee — so every such booking fails.

        The fix is capability, not payload inspection: the identity knows what it
        may do, and the provider trusts it. A regression here would silently return
        the "couldn't be checked" reply to every email-bearing customer, because the
        failure surfaces only in Google's rejection, never in a validation.
        """
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            seen.update(json.loads(request.content))
            return httpx.Response(200, json=event_body(event_id=seen["id"]))

        provider, _ = make_service_account_provider(
            handler, credentials=FakeServiceAccountCredentials()
        )
        request = EventRequest(
            event_id="demo-1",
            interval=TimeInterval(start=START, end=START + timedelta(minutes=30)),
            timezone="Asia/Kolkata",
            summary="Demo",
            description="Email: asha@example.com",
            attendee_emails=("asha@example.com",),
            metadata={"lead_id": "lead-1"},
        )
        event = await provider.create_event(request)

        assert "attendees" not in seen
        assert seen["description"]
        assert seen["extendedProperties"]["private"]["lead_id"] == "lead-1"
        assert event.meeting_link == "https://meet.google.com/abc-defg-hij"

    @pytest.mark.asyncio
    async def test_a_service_account_identity_reports_it_cannot_invite(self) -> None:
        auth = ServiceAccountAuthenticator(credentials=FakeServiceAccountCredentials())
        assert auth.supports_attendees is False

    @pytest.mark.asyncio
    async def test_the_oauth_identity_keeps_the_customer_invite(self) -> None:
        provider, _ = make_provider(routed())
        assert provider._auth.supports_attendees is True  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_a_server_error_is_retryable(self) -> None:
        provider, _ = make_provider(routed(event={"error": {"message": "oops"}}, event_status=503))

        with pytest.raises(RetryableProviderError):
            await provider.create_event(an_event_request())

    @pytest.mark.asyncio
    async def test_a_bad_request_is_permanent(self) -> None:
        provider, _ = make_provider(routed(event={"error": {"message": "bad"}}, event_status=400))

        with pytest.raises(NonRetryableProviderError):
            await provider.create_event(an_event_request())

    @pytest.mark.asyncio
    async def test_a_timeout_is_retryable(self) -> None:
        """Says nothing about whether Google processed it — and the client-supplied
        event ID is what stops the retry double-booking."""

        def handler(request: httpx.Request) -> httpx.Response:
            if "oauth2" in str(request.url):
                return httpx.Response(200, json=token_response())
            raise httpx.ReadTimeout("too slow")

        provider, _ = make_provider(handler)

        with pytest.raises(RetryableProviderError, match="timed out"):
            await provider.create_event(an_event_request())

    @pytest.mark.asyncio
    async def test_an_event_without_times_is_permanent(self) -> None:
        provider, _ = make_provider(routed(event={"id": "abc", "summary": "Demo"}))

        with pytest.raises(NonRetryableProviderError, match="usable time range"):
            await provider.create_event(an_event_request())


STATIC_LINK = "https://meet.google.com/static-room-abc"


def make_static_provider(handler: Any) -> GoogleCalendarProvider:
    provider, _ = make_provider(handler)
    provider._static_meeting_link = STATIC_LINK  # noqa: SLF001 - the test sets what the registry would
    return provider


class TestStaticMeetingLink:
    """The product's permanent meeting link (Option 2, [D65])."""

    @pytest.mark.asyncio
    async def test_no_conference_is_requested_and_the_static_link_is_returned(self) -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if "oauth2" in str(request.url):
                return httpx.Response(200, json=token_response())
            import json

            seen["params"] = dict(request.url.params)
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                200, json=event_body(event_id=seen["body"]["id"], hangoutLink=None)
            )

        event = await make_static_provider(handler).create_event(an_event_request())

        assert "conferenceData" not in seen["body"]
        assert "conferenceDataVersion" not in seen["params"]
        assert seen["body"]["location"] == STATIC_LINK
        assert STATIC_LINK in seen["body"]["description"]
        assert event.meeting_link == STATIC_LINK

    @pytest.mark.asyncio
    async def test_a_calendar_that_refuses_conferences_is_never_asked(self) -> None:
        """With a static link there is no first failing attempt: one call books."""
        calls = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if "oauth2" in str(request.url):
                return httpx.Response(200, json=token_response())
            import json

            calls["count"] += 1
            body = json.loads(request.content)
            assert "conferenceData" not in body
            return httpx.Response(200, json=event_body(event_id=body["id"], hangoutLink=None))

        event = await make_static_provider(handler).create_event(an_event_request())

        assert calls["count"] == 1
        assert event.meeting_link == STATIC_LINK

    @pytest.mark.asyncio
    async def test_create_meeting_false_sends_no_link(self) -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if "oauth2" in str(request.url):
                return httpx.Response(200, json=token_response())
            import json

            seen["body"] = json.loads(request.content)
            return httpx.Response(
                200, json=event_body(event_id=seen["body"]["id"], hangoutLink=None)
            )

        request = EventRequest(
            event_id="demo-abc-123",
            interval=TimeInterval(start=START, end=START + timedelta(minutes=30)),
            timezone="Asia/Kolkata",
            summary="Demo",
            create_meeting=False,
        )
        event = await make_static_provider(handler).create_event(request)

        assert "location" not in seen["body"]
        assert event.meeting_link is None

    @pytest.mark.asyncio
    async def test_a_retry_converges_on_the_static_link(self) -> None:
        """A 409 read-back carries no hangoutLink; the retry still shares one link."""
        first = True

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal first
            if "oauth2" in str(request.url):
                return httpx.Response(200, json=token_response())
            if request.method == "GET":
                return httpx.Response(200, json=event_body(hangoutLink=None))
            if first:
                first = False
                return httpx.Response(409, json={"error": {"message": "duplicate"}})
            raise AssertionError("the retry must read, not recreate")

        event = await make_static_provider(handler).create_event(an_event_request())

        assert event.meeting_link == STATIC_LINK
