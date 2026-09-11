"""Demo slot computation from the [D8] slot policy (specification §12).

Pure logic: no calendar, no database. It takes the policy and a list of busy intervals
and produces the slots that are actually offerable. The calendar *provider* supplies
the busy intervals; this module decides what may be offered, so the rule stays testable
and provider-agnostic.

Built in Phase 10 rather than Phase 11 because the `get_available_slots` and `book_demo`
tools are useless without it and §9 places both in the AI's authorized set. Phase 11
still owns the Google integration, the notification fan-out and the booking record.

Every value here comes from configuration ([D8]): duration, buffer, working days and
hours, horizon and timezone. None of them is a literal in this file.

Two rules matter more than they look:

- **The buffer applies on both sides of a busy interval.** A demo that starts the
  instant another ends leaves nobody time to move between them, so a busy interval is
  padded before it is compared.
- **Everything is computed in the sales team's timezone, then returned as UTC-aware
  instants.** "10:00 to 18:00" is a local statement, and a customer in another timezone
  must still be offered the same working hours.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Protocol, TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.integrations.calendar import TimeInterval

# Weekday names as configuration uses them, in `date.weekday()` order.
WEEKDAY_NAMES = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")


class SlotPolicyError(ValueError):
    """The configured slot policy cannot be interpreted."""


@dataclass(frozen=True, slots=True)
class SlotPolicy:
    """The [D8] policy, parsed and validated once.

    Constructed from `Settings` by `policy_from_settings`, so the values live in
    configuration and nothing downstream carries a literal.
    """

    duration_minutes: int
    buffer_minutes: int
    working_days: frozenset[int]
    working_start: time
    working_end: time
    horizon_days: int
    timezone: str

    def __post_init__(self) -> None:
        if self.duration_minutes <= 0:
            raise SlotPolicyError("Demo duration must be positive")
        if self.buffer_minutes < 0:
            raise SlotPolicyError("Demo buffer cannot be negative")
        if self.horizon_days <= 0:
            raise SlotPolicyError("Booking horizon must be at least one day")
        if not self.working_days:
            raise SlotPolicyError("At least one working day is required")
        if self.working_end <= self.working_start:
            raise SlotPolicyError("Working hours must end after they start")

        span = _minutes_between(self.working_start, self.working_end)
        if span < self.duration_minutes:
            raise SlotPolicyError(
                f"A {self.duration_minutes}-minute demo does not fit in a {span}-minute working day"
            )

    @property
    def zone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise SlotPolicyError(f"Unknown timezone {self.timezone!r}") from exc


def parse_working_days(values: list[str]) -> frozenset[int]:
    """Turn `MON,TUE,...` into `date.weekday()` numbers."""
    days: set[int] = set()

    for raw in values:
        name = raw.strip().upper()[:3]
        if name not in WEEKDAY_NAMES:
            raise SlotPolicyError(f"Unknown working day {raw!r}")
        days.add(WEEKDAY_NAMES.index(name))

    return frozenset(days)


def parse_clock_time(value: str) -> time:
    """Parse `HH:MM` from configuration."""
    try:
        hour, minute = (int(part) for part in value.strip().split(":", 1))
        return time(hour=hour, minute=minute)
    except (ValueError, TypeError) as exc:
        raise SlotPolicyError(f"Working hours must look like HH:MM, got {value!r}") from exc


def available_slots(
    *,
    policy: SlotPolicy,
    busy: list[TimeInterval],
    now: datetime | None = None,
    limit: int | None = None,
) -> list[TimeInterval]:
    """Slots that may be offered, earliest first.

    A slot is offerable when it falls on a working day, sits inside working hours,
    starts in the future, and does not collide with a busy interval once the buffer is
    applied to both.

    `limit` caps the result because these are read out to a customer over WhatsApp;
    offering forty slots is not a choice, it is a wall of text.
    """
    moment = (now or datetime.now(UTC)).astimezone(policy.zone)
    padded = [_pad(interval, policy.buffer_minutes) for interval in busy]

    duration = timedelta(minutes=policy.duration_minutes)
    # Candidates start on a grid of the demo's own length. A finer grid would offer
    # 10:00, 10:05, 10:10 — technically free, useless to choose between.
    step = duration

    slots: list[TimeInterval] = []
    for day_offset in range(policy.horizon_days):
        day = (moment + timedelta(days=day_offset)).date()
        if day.weekday() not in policy.working_days:
            continue

        for candidate in _day_candidates(day, policy, step, duration):
            if candidate.start <= moment:
                # Already past, or starting this instant. Offering it would produce a
                # booking nobody can attend.
                continue
            if any(candidate.overlaps(interval) for interval in padded):
                continue

            slots.append(
                TimeInterval(
                    start=candidate.start.astimezone(UTC), end=candidate.end.astimezone(UTC)
                )
            )
            if limit is not None and len(slots) >= limit:
                return slots

    return slots


def is_slot_available(
    *,
    policy: SlotPolicy,
    slot: TimeInterval,
    busy: list[TimeInterval],
    now: datetime | None = None,
) -> bool:
    """Whether one specific interval may still be booked.

    §12 requires a re-check immediately before booking: the slots were computed when
    the customer was offered them, and someone else may have taken one while they
    thought about it. This is that check.
    """
    moment = (now or datetime.now(UTC)).astimezone(policy.zone)
    local_start = slot.start.astimezone(policy.zone)
    local_end = slot.end.astimezone(policy.zone)

    if local_start <= moment:
        return False
    if int((local_end - local_start).total_seconds() // 60) != policy.duration_minutes:
        # A slot of the wrong length was not one this policy offered.
        return False
    if local_start.date().weekday() not in policy.working_days:
        return False
    if local_start.time() < policy.working_start or local_end.time() > policy.working_end:
        return False
    if local_start.date() != local_end.date():
        # A slot spanning midnight cannot sit inside one working day.
        return False
    if (local_start.date() - moment.date()).days >= policy.horizon_days:
        return False

    padded = [_pad(interval, policy.buffer_minutes) for interval in busy]
    return not any(slot.overlaps(interval) for interval in padded)


# ---------------------------------------------------------------- internals


def _day_candidates(
    day: date, policy: SlotPolicy, step: timedelta, duration: timedelta
) -> list[TimeInterval]:
    start = datetime.combine(day, policy.working_start, tzinfo=policy.zone)
    closing = datetime.combine(day, policy.working_end, tzinfo=policy.zone)

    candidates: list[TimeInterval] = []
    cursor = start
    while cursor + duration <= closing:
        candidates.append(TimeInterval(start=cursor, end=cursor + duration))
        cursor += step

    return candidates


def _pad(interval: TimeInterval, buffer_minutes: int) -> TimeInterval:
    """Widen a busy interval by the buffer on both sides.

    Both sides on purpose: back-to-back demos leave no room to finish one and prepare
    for the next, and the gap is needed before as much as after.
    """
    if buffer_minutes <= 0:
        return interval

    padding = timedelta(minutes=buffer_minutes)
    return TimeInterval(start=interval.start - padding, end=interval.end + padding)


def _minutes_between(start: time, end: time) -> int:
    return (end.hour * 60 + end.minute) - (start.hour * 60 + start.minute)


__all__ = [
    "WEEKDAY_NAMES",
    "SlotPolicy",
    "SlotPolicyError",
    "SlotPolicySettings",
    "available_slots",
    "is_slot_available",
    "parse_clock_time",
    "parse_working_days",
    "policy_from_settings",
    "resolve_slot_policy",
    "SlotPolicyOverrides",
]


class SlotPolicySettings(Protocol):
    """The configuration this module reads.

    A protocol rather than an import of `Settings`, so `app.domain` does not depend on
    `app.core.config` and the layering stays one-way — while still being type-checked
    rather than duck-typed through `getattr`.
    """

    demo_duration_minutes: int
    demo_buffer_minutes: int
    demo_working_days: list[str]
    demo_working_hours_start: str
    demo_working_hours_end: str
    demo_booking_horizon_days: int
    demo_timezone: str


class SlotPolicyOverrides(Protocol):
    """A product's sparse overrides of the global policy ([D52]).

    Read-only properties rather than attributes, so an ORM row satisfies it directly.
    Every value `None` means inherit — a product whose hours match the platform's states
    nothing.
    """

    @property
    def demo_duration_minutes(self) -> int | None: ...
    @property
    def demo_buffer_minutes(self) -> int | None: ...
    @property
    def demo_working_days(self) -> list[str] | None: ...
    @property
    def demo_working_hours_start(self) -> str | None: ...
    @property
    def demo_working_hours_end(self) -> str | None: ...
    @property
    def demo_booking_horizon_days(self) -> int | None: ...
    @property
    def demo_timezone(self) -> str | None: ...


_T = TypeVar("_T")


def _inherit(override: _T | None, default: _T) -> _T:
    # `is None`, never truthiness: a zero-minute buffer is a real override, and `0 or 15`
    # would quietly put the platform's buffer back.
    return default if override is None else override


def resolve_slot_policy(
    settings: SlotPolicySettings, overrides: SlotPolicyOverrides | None
) -> SlotPolicy:
    """Product override, then global default, field by field ([D8], [D52]).

    Resolved per field rather than as a whole policy, so a product that changes only its
    timezone keeps the platform's duration, buffer, days, hours and horizon.

    The merged policy is validated as one, by `SlotPolicy` itself: an override that is fine
    alone can be invalid in combination — a 90-minute demo in a product whose day is an
    hour long — and that must fail rather than offer slots that cannot be booked.
    """
    working_days = _inherit(
        overrides.demo_working_days if overrides else None, settings.demo_working_days
    )
    if not isinstance(working_days, list):
        # JSONB will hold whatever it is given; a string here would be parsed a letter
        # at a time and fail confusingly.
        raise SlotPolicyError("Working days must be a list such as ['MON', 'TUE']")

    policy = SlotPolicy(
        duration_minutes=_inherit(
            overrides.demo_duration_minutes if overrides else None,
            settings.demo_duration_minutes,
        ),
        buffer_minutes=_inherit(
            overrides.demo_buffer_minutes if overrides else None, settings.demo_buffer_minutes
        ),
        working_days=parse_working_days(working_days),
        working_start=parse_clock_time(
            _inherit(
                overrides.demo_working_hours_start if overrides else None,
                settings.demo_working_hours_start,
            )
        ),
        working_end=parse_clock_time(
            _inherit(
                overrides.demo_working_hours_end if overrides else None,
                settings.demo_working_hours_end,
            )
        ),
        horizon_days=_inherit(
            overrides.demo_booking_horizon_days if overrides else None,
            settings.demo_booking_horizon_days,
        ),
        timezone=_inherit(overrides.demo_timezone if overrides else None, settings.demo_timezone),
    )
    # Checked now rather than when a slot is first formatted: an unknown zone found inside a
    # booking tool would fail the customer's turn instead of making booking unavailable.
    _ = policy.zone
    return policy


def policy_from_settings(settings: SlotPolicySettings) -> SlotPolicy:
    """Build the platform-wide policy from configuration ([D8]).

    The same as resolving with no product overrides, and implemented that way so there is
    one rule rather than two that could drift.
    """
    return resolve_slot_policy(settings, None)
