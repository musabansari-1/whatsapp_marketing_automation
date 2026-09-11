"""Calendar provider interface (specification §12, [D7], [D19]).

    CalendarProvider
     ├── GoogleCalendarProvider  (Phase 11)
     └── FakeCalendarProvider

The provider is deliberately thin: it reports busy intervals and mutates events.
Turning busy intervals into offerable slots is domain logic driven by the [D8] slot
policy, so working hours and buffers never leak into an integration.

Idempotency (§20): `create_event` takes a caller-supplied `event_id`. Google accepts
client-generated event IDs, which is what prevents a retried booking task from
creating a second calendar event — the failure mode §12 explicitly forbids.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class TimeInterval:
    """A half-open interval [start, end). Both ends are timezone-aware."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("TimeInterval requires timezone-aware datetimes")
        if self.end <= self.start:
            raise ValueError("TimeInterval end must be after start")

    def overlaps(self, other: TimeInterval) -> bool:
        return self.start < other.end and other.start < self.end


@dataclass(frozen=True, slots=True)
class CalendarEvent:
    """A created calendar event. `event_id` is persisted per §12."""

    event_id: str
    interval: TimeInterval
    timezone: str
    summary: str
    attendee_emails: tuple[str, ...] = ()
    meeting_link: str | None = None


@dataclass(frozen=True, slots=True)
class EventRequest:
    """A booking request.

    `event_id` is client-generated so retries converge on one event.
    """

    event_id: str
    interval: TimeInterval
    timezone: str
    summary: str
    description: str | None = None
    attendee_emails: tuple[str, ...] = ()
    metadata: dict[str, str] = field(default_factory=dict)


class SlotUnavailableError(Exception):
    """The requested interval is no longer free.

    Raised by `create_event` when the provider detects the race §12 requires us to
    handle: the slot was taken between presentation and confirmation.
    """


class CalendarProvider(ABC):
    name: str

    @abstractmethod
    async def list_busy_intervals(self, window: TimeInterval) -> list[TimeInterval]:
        """Return busy intervals on this provider's calendar within `window`.

        One provider instance is one calendar, and since Phase 24 that is a product's own
        ([D43]) — so these are that product's commitments and nobody else's.

        Never invent availability (§12): an empty list means genuinely free.
        """

    @abstractmethod
    async def create_event(self, request: EventRequest) -> CalendarEvent:
        """Create the event, or raise `SlotUnavailableError` if the slot was taken.

        Returning normally is the provider confirmation the booking flow waits for; no
        booking may be persisted or announced before it.
        """

    @abstractmethod
    async def get_event(self, event_id: str) -> CalendarEvent | None:
        """Fetch an event by ID, or None. Lets a retry detect its own prior success."""

    @abstractmethod
    async def delete_event(self, event_id: str) -> None:
        """Delete an event. Idempotent: deleting an absent event is not an error."""
