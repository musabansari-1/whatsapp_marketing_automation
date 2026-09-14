"""Demo slot computation tests (specification §12, [D8]).

Every value is configuration, so the tests build explicit policies rather than relying
on the defaults — a test that passed only because the default happened to suit it would
not be testing the rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.domain.slots import (
    SlotPolicy,
    SlotPolicyError,
    available_slots,
    is_slot_available,
    parse_clock_time,
    parse_working_days,
    policy_from_settings,
    resolve_slot_policy,
)
from app.integrations.calendar import TimeInterval

IST = ZoneInfo("Asia/Kolkata")


def a_policy(
    *,
    duration: int = 30,
    buffer: int = 15,
    days: frozenset[int] | None = None,
    start: time = time(10, 0),
    end: time = time(18, 0),
    horizon: int | None = 14,
    min_lead: int = 0,
    timezone: str = "Asia/Kolkata",
) -> SlotPolicy:
    return SlotPolicy(
        duration_minutes=duration,
        buffer_minutes=buffer,
        working_days=days if days is not None else frozenset({0, 1, 2, 3, 4}),
        working_start=start,
        working_end=end,
        horizon_days=horizon,
        min_lead_days=min_lead,
        timezone=timezone,
    )


def local(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=IST)


def busy(start: datetime, minutes: int) -> TimeInterval:
    return TimeInterval(start=start, end=start + timedelta(minutes=minutes))


# A Wednesday, so the default working days apply without a weekend in the way.
WEDNESDAY_9AM = local(2026, 9, 2, 9, 0)


class TestParsing:
    def test_working_days_parse(self) -> None:
        assert parse_working_days(["MON", "WED", "FRI"]) == frozenset({0, 2, 4})

    def test_working_days_are_case_and_length_tolerant(self) -> None:
        """Configuration is typed by a human; 'Monday' and 'mon' mean the same day."""
        assert parse_working_days(["monday", "Tue"]) == frozenset({0, 1})

    def test_an_unknown_day_is_rejected(self) -> None:
        with pytest.raises(SlotPolicyError, match="Funday"):
            parse_working_days(["Funday"])

    def test_clock_times_parse(self) -> None:
        assert parse_clock_time("09:30") == time(9, 30)

    @pytest.mark.parametrize("value", ["9am", "25:00", "", "half past nine"])
    def test_a_malformed_clock_time_is_rejected(self, value: str) -> None:
        with pytest.raises(SlotPolicyError):
            parse_clock_time(value)


class TestPolicyValidation:
    def test_a_valid_policy_is_accepted(self) -> None:
        assert a_policy().duration_minutes == 30

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"duration": 0},
            {"buffer": -5},
            {"horizon": 0},
            {"days": frozenset()},
            {"start": time(18, 0), "end": time(10, 0)},
        ],
    )
    def test_an_invalid_policy_is_refused(self, kwargs: dict[str, object]) -> None:
        with pytest.raises(SlotPolicyError):
            a_policy(**kwargs)  # type: ignore[arg-type]

    def test_a_demo_longer_than_the_working_day_is_refused(self) -> None:
        """Caught at construction rather than by silently offering nothing, which
        would look like a full calendar."""
        with pytest.raises(SlotPolicyError, match="does not fit"):
            a_policy(duration=600, start=time(10, 0), end=time(12, 0))

    def test_an_unknown_timezone_is_refused(self) -> None:
        with pytest.raises(SlotPolicyError, match="Unknown timezone"):
            _ = a_policy(timezone="Mars/Olympus_Mons").zone

    def test_the_configured_timezone_resolves(self) -> None:
        """`tzdata` is a dependency precisely so this works on any host: Windows has no
        system zoneinfo and a slim image is not guaranteed to carry it."""
        assert a_policy().zone.key == "Asia/Kolkata"


class TestAvailability:
    def test_an_empty_calendar_offers_the_working_day(self) -> None:
        slots = available_slots(policy=a_policy(), busy=[], now=WEDNESDAY_9AM)

        first = slots[0].start.astimezone(IST)
        assert first.hour == 10
        assert first.minute == 0

    def test_slots_are_the_configured_length(self) -> None:
        slots = available_slots(policy=a_policy(duration=45), busy=[], now=WEDNESDAY_9AM)
        assert slots[0].end - slots[0].start == timedelta(minutes=45)

    def test_nothing_before_working_hours_is_offered(self) -> None:
        slots = available_slots(policy=a_policy(), busy=[], now=WEDNESDAY_9AM, limit=20)

        for slot in slots:
            assert slot.start.astimezone(IST).time() >= time(10, 0)

    def test_nothing_runs_past_closing(self) -> None:
        slots = available_slots(policy=a_policy(), busy=[], now=WEDNESDAY_9AM, limit=50)

        for slot in slots:
            assert slot.end.astimezone(IST).time() <= time(18, 0)

    def test_a_slot_in_the_past_is_never_offered(self) -> None:
        """Offering it produces a booking nobody can attend."""
        midday = local(2026, 9, 2, 14, 0)
        slots = available_slots(policy=a_policy(), busy=[], now=midday)

        assert all(slot.start > midday for slot in slots)

    def test_non_working_days_are_skipped(self) -> None:
        friday_evening = local(2026, 9, 4, 17, 45)
        slots = available_slots(policy=a_policy(), busy=[], now=friday_evening, limit=3)

        # Saturday and Sunday are absent, so the next offer is Monday.
        assert slots[0].start.astimezone(IST).date() == local(2026, 9, 7, 10).date()

    def test_a_busy_interval_removes_its_slot(self) -> None:
        policy = a_policy(buffer=0)
        occupied = busy(local(2026, 9, 2, 10, 0), 30)

        slots = available_slots(policy=policy, busy=[occupied], now=WEDNESDAY_9AM, limit=3)

        assert all(slot.start.astimezone(IST) != local(2026, 9, 2, 10, 0) for slot in slots)

    def test_the_buffer_applies_on_both_sides(self) -> None:
        """A demo starting the instant another ends leaves nobody time to move between
        them — and the gap is needed before as much as after."""
        policy = a_policy(buffer=15)
        occupied = busy(local(2026, 9, 2, 11, 0), 30)

        starts = {
            slot.start.astimezone(IST)
            for slot in available_slots(policy=policy, busy=[occupied], now=WEDNESDAY_9AM, limit=20)
        }

        assert local(2026, 9, 2, 10, 30) not in starts  # ends 11:00, too close after
        assert local(2026, 9, 2, 11, 30) not in starts  # starts as the meeting ends
        assert local(2026, 9, 2, 10, 0) in starts  # ends 10:30, clear of the buffer

    def test_a_zero_buffer_permits_back_to_back(self) -> None:
        policy = a_policy(buffer=0)
        occupied = busy(local(2026, 9, 2, 11, 0), 30)

        starts = {
            slot.start.astimezone(IST)
            for slot in available_slots(policy=policy, busy=[occupied], now=WEDNESDAY_9AM, limit=20)
        }

        assert local(2026, 9, 2, 11, 30) in starts

    def test_a_full_day_offers_nothing_that_day(self) -> None:
        policy = a_policy(buffer=0)
        all_day = busy(local(2026, 9, 2, 10, 0), 8 * 60)

        slots = available_slots(policy=policy, busy=[all_day], now=WEDNESDAY_9AM, limit=5)

        assert all(slot.start.astimezone(IST).date() != WEDNESDAY_9AM.date() for slot in slots)

    def test_the_horizon_is_respected(self) -> None:
        policy = a_policy(horizon=2)
        slots = available_slots(policy=policy, busy=[], now=WEDNESDAY_9AM, limit=100)

        latest = max(slot.start.astimezone(IST).date() for slot in slots)
        assert (latest - WEDNESDAY_9AM.date()).days < 2

    def test_the_limit_is_respected(self) -> None:
        """These are read out over WhatsApp; forty options is a wall of text, not a
        choice."""
        slots = available_slots(policy=a_policy(), busy=[], now=WEDNESDAY_9AM, limit=3)
        assert len(slots) == 3

    def test_slots_are_returned_in_utc(self) -> None:
        """Stored and compared as instants; the local rendering is for the customer."""
        slots = available_slots(policy=a_policy(), busy=[], now=WEDNESDAY_9AM, limit=1)
        assert slots[0].start.tzinfo is UTC

    def test_slots_are_ordered_earliest_first(self) -> None:
        slots = available_slots(policy=a_policy(), busy=[], now=WEDNESDAY_9AM, limit=10)
        assert [slot.start for slot in slots] == sorted(slot.start for slot in slots)

    def test_working_hours_are_local_not_utc(self) -> None:
        """ "10:00 to 18:00" is a statement about the sales team's day. A customer in
        another timezone must still be offered those hours."""
        slots = available_slots(policy=a_policy(), busy=[], now=WEDNESDAY_9AM, limit=1)

        assert slots[0].start.astimezone(IST).hour == 10
        # 10:00 IST is 04:30 UTC, which is what makes this worth asserting.
        assert slots[0].start.hour == 4


class TestSlotRecheck:
    """§12 requires re-checking immediately before booking: the customer chose from a
    list computed minutes ago, and someone else may have taken it since."""

    def test_a_free_slot_is_available(self) -> None:
        slot = busy(local(2026, 9, 2, 11, 0), 30)
        assert is_slot_available(policy=a_policy(), slot=slot, busy=[], now=WEDNESDAY_9AM)

    def test_a_taken_slot_is_not(self) -> None:
        slot = busy(local(2026, 9, 2, 11, 0), 30)
        assert not is_slot_available(policy=a_policy(), slot=slot, busy=[slot], now=WEDNESDAY_9AM)

    def test_a_slot_inside_the_buffer_is_not(self) -> None:
        slot = busy(local(2026, 9, 2, 11, 0), 30)
        neighbour = busy(local(2026, 9, 2, 11, 30), 30)

        assert not is_slot_available(
            policy=a_policy(buffer=15), slot=slot, busy=[neighbour], now=WEDNESDAY_9AM
        )

    def test_a_past_slot_is_not(self) -> None:
        slot = busy(local(2026, 9, 2, 8, 0), 30)
        assert not is_slot_available(policy=a_policy(), slot=slot, busy=[], now=WEDNESDAY_9AM)

    def test_a_slot_outside_working_hours_is_not(self) -> None:
        slot = busy(local(2026, 9, 2, 19, 0), 30)
        assert not is_slot_available(policy=a_policy(), slot=slot, busy=[], now=WEDNESDAY_9AM)

    def test_a_slot_on_a_non_working_day_is_not(self) -> None:
        saturday = busy(local(2026, 9, 5, 11, 0), 30)
        assert not is_slot_available(policy=a_policy(), slot=saturday, busy=[], now=WEDNESDAY_9AM)

    def test_a_slot_of_the_wrong_length_is_not(self) -> None:
        """A two-hour "demo" was not something this policy offered, so it is not
        something a customer can have chosen."""
        slot = busy(local(2026, 9, 2, 11, 0), 120)
        assert not is_slot_available(policy=a_policy(), slot=slot, busy=[], now=WEDNESDAY_9AM)

    def test_a_slot_beyond_the_horizon_is_not(self) -> None:
        far = busy(local(2026, 10, 30, 11, 0), 30)
        assert not is_slot_available(policy=a_policy(), slot=far, busy=[], now=WEDNESDAY_9AM)

    def test_every_offered_slot_passes_the_recheck(self) -> None:
        """The two functions must agree, or the AI offers times that booking then
        refuses."""
        policy = a_policy()
        occupied = [busy(local(2026, 9, 2, 12, 0), 60), busy(local(2026, 9, 3, 10, 0), 30)]

        for slot in available_slots(policy=policy, busy=occupied, now=WEDNESDAY_9AM, limit=20):
            assert is_slot_available(policy=policy, slot=slot, busy=occupied, now=WEDNESDAY_9AM), (
                slot
            )


class TestPolicyFromSettings:
    def test_the_policy_comes_from_configuration(self) -> None:
        """[D8]: none of these is a literal anywhere in the slot code."""
        from app.core.config import Settings

        policy = policy_from_settings(Settings())

        assert policy.duration_minutes == Settings().demo_duration_minutes
        assert policy.timezone == Settings().demo_timezone
        assert policy.working_days == parse_working_days(Settings().demo_working_days)


@dataclass
class Overrides:
    """A product's slot-policy columns. Every field None means inherit ([D52])."""

    demo_duration_minutes: int | None = None
    demo_buffer_minutes: int | None = None
    demo_working_days: list[str] | None = None
    demo_working_hours_start: str | None = None
    demo_working_hours_end: str | None = None
    demo_booking_horizon_days: int | None = None
    demo_min_lead_days: int | None = None
    demo_timezone: str | None = None


class TestProductOverrides:
    """[D52]: product override, then global default, resolved field by field."""

    def test_no_overrides_is_exactly_the_platform_policy(self) -> None:
        from app.core.config import Settings

        assert resolve_slot_policy(Settings(), Overrides()) == policy_from_settings(Settings())

    def test_one_override_changes_only_that_field(self) -> None:
        from app.core.config import Settings

        platform = policy_from_settings(Settings())
        resolved = resolve_slot_policy(Settings(), Overrides(demo_timezone="Europe/London"))

        assert resolved.timezone == "Europe/London"
        assert resolved.duration_minutes == platform.duration_minutes
        assert resolved.buffer_minutes == platform.buffer_minutes
        assert resolved.working_days == platform.working_days
        assert resolved.working_start == platform.working_start
        assert resolved.working_end == platform.working_end
        assert resolved.horizon_days == platform.horizon_days
        assert resolved.min_lead_days == platform.min_lead_days

    def test_a_zero_buffer_is_a_real_override(self) -> None:
        """Resolved with `is None`, not truthiness: `0 or 15` would restore the platform's
        buffer and silently ignore the product's setting."""
        from app.core.config import Settings

        assert resolve_slot_policy(Settings(), Overrides(demo_buffer_minutes=0)).buffer_minutes == 0

    def test_every_field_can_be_overridden(self) -> None:
        from app.core.config import Settings

        resolved = resolve_slot_policy(
            Settings(),
            Overrides(
                demo_duration_minutes=45,
                demo_buffer_minutes=5,
                demo_working_days=["SAT", "SUN"],
                demo_working_hours_start="09:00",
                demo_working_hours_end="13:00",
                demo_booking_horizon_days=7,
                demo_min_lead_days=1,
                demo_timezone="Asia/Dubai",
            ),
        )

        assert resolved == SlotPolicy(
            duration_minutes=45,
            buffer_minutes=5,
            working_days=parse_working_days(["SAT", "SUN"]),
            working_start=time(9, 0),
            working_end=time(13, 0),
            horizon_days=7,
            min_lead_days=1,
            timezone="Asia/Dubai",
        )

    def test_unbounded_horizon_override_maps_to_none(self) -> None:
        from app.core.config import Settings

        resolved = resolve_slot_policy(Settings(), Overrides(demo_booking_horizon_days=-1))
        assert resolved.horizon_days is None
        assert resolved.is_horizon_unbounded

    def test_min_lead_days_override_is_applied(self) -> None:
        from app.core.config import Settings

        resolved = resolve_slot_policy(Settings(), Overrides(demo_min_lead_days=1))
        assert resolved.min_lead_days == 1

    def test_a_combination_that_cannot_fit_is_refused(self) -> None:
        """Valid alone, invalid together: a 30-minute demo in a 15-minute day. Refused,
        rather than offering slots that cannot be booked."""
        from app.core.config import Settings

        with pytest.raises(SlotPolicyError):
            resolve_slot_policy(
                Settings(),
                Overrides(demo_working_hours_start="10:00", demo_working_hours_end="10:15"),
            )

    def test_working_days_must_be_a_list(self) -> None:
        """JSONB holds whatever it is given; a string would otherwise be read a letter at
        a time."""
        from app.core.config import Settings

        with pytest.raises(SlotPolicyError, match="list"):
            resolve_slot_policy(Settings(), Overrides(demo_working_days="MON,TUE"))  # type: ignore[arg-type]

    def test_an_unknown_timezone_is_refused_when_resolved(self) -> None:
        from app.core.config import Settings

        with pytest.raises(SlotPolicyError, match="timezone"):
            resolve_slot_policy(Settings(), Overrides(demo_timezone="Mars/Olympus_Mons"))


class TestMinLeadAndUnboundedHorizon:
    def test_min_lead_skips_today_and_offers_tomorrow(self) -> None:
        policy = a_policy(min_lead=1, days=frozenset({0, 1, 2, 3, 4, 5, 6}))
        slots = available_slots(policy=policy, busy=[], now=WEDNESDAY_9AM, limit=5)

        assert slots
        first_day = slots[0].start.astimezone(IST).date()
        assert first_day == (WEDNESDAY_9AM.date() + timedelta(days=1))
        assert all(slot.start.astimezone(IST).date() >= first_day for slot in slots)

        today_slot = busy(local(2026, 9, 2, 11, 0), 30)
        tomorrow_slot = busy(local(2026, 9, 3, 11, 0), 30)
        assert not is_slot_available(policy=policy, slot=today_slot, busy=[], now=WEDNESDAY_9AM)
        assert is_slot_available(policy=policy, slot=tomorrow_slot, busy=[], now=WEDNESDAY_9AM)

    def test_unbounded_horizon_can_search_far_ahead(self) -> None:
        policy = a_policy(horizon=None, days=frozenset({0, 1, 2, 3, 4, 5, 6}))
        search_from = (WEDNESDAY_9AM + timedelta(days=60)).date()

        slots = available_slots(
            policy=policy,
            busy=[],
            now=WEDNESDAY_9AM,
            search_from=search_from,
            search_days=7,
            limit=3,
        )
        assert slots
        assert all(slot.start.astimezone(IST).date() >= search_from for slot in slots)

        far_slot = busy(local(2026, 11, 1, 11, 0), 30)
        assert is_slot_available(policy=policy, slot=far_slot, busy=[], now=WEDNESDAY_9AM)

    def test_academicae_like_hours_still_offer_slots(self) -> None:
        """60-minute demos, 15-minute buffer, 08:00–18:00 every day."""
        policy = a_policy(
            duration=60,
            buffer=15,
            start=time(8, 0),
            end=time(18, 0),
            days=frozenset({0, 1, 2, 3, 4, 5, 6}),
            min_lead=1,
            horizon=None,
        )
        slots = available_slots(policy=policy, busy=[], now=WEDNESDAY_9AM, limit=5)
        assert slots
        assert all(slot.end - slot.start == timedelta(minutes=60) for slot in slots)
        assert all(slot.start.astimezone(IST).time() >= time(8, 0) for slot in slots)
        assert all(slot.end.astimezone(IST).time() <= time(18, 0) for slot in slots)
