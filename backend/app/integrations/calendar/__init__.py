"""Calendar integration package."""

from app.integrations.calendar.fake import FakeCalendarProvider
from app.integrations.calendar.provider import (
    CalendarEvent,
    CalendarProvider,
    EventRequest,
    SlotUnavailableError,
    TimeInterval,
)

__all__ = [
    "CalendarEvent",
    "CalendarProvider",
    "EventRequest",
    "FakeCalendarProvider",
    "SlotUnavailableError",
    "TimeInterval",
]
