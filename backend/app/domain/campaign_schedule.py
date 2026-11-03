"""Scheduled broadcast rules.

Pure: the caller says what time it is and when the campaign should go, and this
decides. No database, no queue.

A scheduled campaign is the same human-confirmed broadcast as "Send broadcast" (§7),
with the moment moved. Two things make it different, and both are decided here:

- **When a time may be chosen.** Far enough ahead that the confirmation is a decision
  rather than a race with the clock, and not past the queue's horizon: Cloud Tasks
  refuses a task more than 30 days out, and a schedule the queue cannot hold would
  rest on the sweep alone.
- **When it is too late to send.** If every trigger was down past the chosen moment
  (a worker outage, a paused scheduler), sending hours late is worse than not
  sending: a marketing message chosen for 11:00 arriving at 23:00 is a different
  message. Past the lateness limit the campaign goes back to READY and a person
  decides again.
"""

from __future__ import annotations

from datetime import datetime, timedelta

#: The earliest a schedule may be set, measured from now.
MIN_LEAD = timedelta(minutes=2)
#: Cloud Tasks holds a task at most 30 days ahead; the margin keeps a schedule set
#: at the boundary from being refused by the queue a moment later.
MAX_AHEAD = timedelta(days=29)


class ScheduleTimeError(ValueError):
    """The requested send time cannot be scheduled."""


def assert_schedulable_time(when: datetime, now: datetime) -> None:
    """Refuse a send time that is naive, too soon or too far ahead."""
    if when.tzinfo is None or when.utcoffset() is None:
        raise ScheduleTimeError("The send time must include a timezone")
    if when < now + MIN_LEAD:
        raise ScheduleTimeError(
            "Pick a time at least 2 minutes from now, or use Send broadcast to send now"
        )
    if when > now + MAX_AHEAD:
        raise ScheduleTimeError("A campaign can be scheduled at most 29 days ahead")


def is_due(scheduled_for: datetime | None, now: datetime) -> bool:
    """Whether the chosen moment has arrived.

    A missing time is never due: a SCHEDULED campaign without one is a data error, and
    starting it would send at a moment nobody chose.
    """
    return scheduled_for is not None and scheduled_for <= now


def is_too_late(scheduled_for: datetime, now: datetime, max_lateness: timedelta) -> bool:
    """Whether the moment has passed by more than the lateness limit."""
    return now - scheduled_for > max_lateness


__all__ = [
    "MAX_AHEAD",
    "MIN_LEAD",
    "ScheduleTimeError",
    "assert_schedulable_time",
    "is_due",
    "is_too_late",
]
