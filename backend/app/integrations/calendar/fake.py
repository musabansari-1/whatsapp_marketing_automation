"""In-memory calendar for automated tests.

Phase 11 must prove two things without touching Google: that a taken slot is
rejected, and that a retried booking does not create a duplicate event. This fake
enforces both — it refuses overlapping intervals and treats a repeated `event_id` as
a replay of the same event.
"""

from __future__ import annotations

from app.core.errors import RetryableProviderError
from app.integrations.calendar.provider import (
    CalendarEvent,
    CalendarProvider,
    EventRequest,
    SlotUnavailableError,
    TimeInterval,
)


class FakeCalendarProvider(CalendarProvider):
    name = "fake"

    def __init__(
        self,
        *,
        busy: list[TimeInterval] | None = None,
        fail_with: Exception | None = None,
        calendar_id: str | None = None,
    ) -> None:
        # Which calendar this stands in for. Carried so a test can see which product's
        # calendar a turn was given ([D43]); the fake itself behaves the same either way.
        self.calendar_id = calendar_id
        self.busy: list[TimeInterval] = list(busy or [])
        self.fail_with = fail_with
        self.events: dict[str, CalendarEvent] = {}
        self.created_requests: list[EventRequest] = []
        self.deleted_event_ids: list[str] = []

    def _maybe_fail(self) -> None:
        if self.fail_with is not None:
            raise self.fail_with

    async def list_busy_intervals(self, window: TimeInterval) -> list[TimeInterval]:
        self._maybe_fail()
        booked = [event.interval for event in self.events.values()]
        return [interval for interval in [*self.busy, *booked] if interval.overlaps(window)]

    async def create_event(self, request: EventRequest) -> CalendarEvent:
        self._maybe_fail()

        # A retry of a task that already succeeded must converge, not duplicate (§12).
        existing = self.events.get(request.event_id)
        if existing is not None:
            return existing

        for interval in [*self.busy, *(event.interval for event in self.events.values())]:
            if interval.overlaps(request.interval):
                raise SlotUnavailableError(
                    f"interval {request.interval.start.isoformat()} is no longer available"
                )

        event = CalendarEvent(
            event_id=request.event_id,
            interval=request.interval,
            timezone=request.timezone,
            summary=request.summary,
            attendee_emails=request.attendee_emails,
        )
        self.events[event.event_id] = event
        self.created_requests.append(request)
        return event

    async def get_event(self, event_id: str) -> CalendarEvent | None:
        self._maybe_fail()
        return self.events.get(event_id)

    async def delete_event(self, event_id: str) -> None:
        self._maybe_fail()
        self.events.pop(event_id, None)
        self.deleted_event_ids.append(event_id)

    # ---------------------------------------------------------------- test helpers

    def mark_busy(self, interval: TimeInterval) -> None:
        """Make an interval unavailable, simulating an externally created event."""
        self.busy.append(interval)

    @property
    def event_count(self) -> int:
        return len(self.events)

    def reset(self) -> None:
        self.busy.clear()
        self.events.clear()
        self.created_requests.clear()
        self.deleted_event_ids.clear()

    def fail_retryably(self, message: str = "simulated Calendar outage") -> None:
        self.fail_with = RetryableProviderError(message, provider=self.name)

    def stop_failing(self) -> None:
        self.fail_with = None
