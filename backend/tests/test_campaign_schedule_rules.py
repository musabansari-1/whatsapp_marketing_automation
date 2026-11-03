"""Scheduled broadcast rules: which moments may be chosen, and when it is too late."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.domain.campaign_schedule import (
    MAX_AHEAD,
    MIN_LEAD,
    ScheduleTimeError,
    assert_schedulable_time,
    is_due,
    is_too_late,
)

NOW = datetime(2026, 11, 4, 6, 0, tzinfo=UTC)
IST = timezone(timedelta(hours=5, minutes=30))


class TestSchedulableTime:
    def test_a_time_comfortably_ahead_is_accepted(self) -> None:
        assert_schedulable_time(NOW + timedelta(hours=3), NOW)

    def test_the_minimum_lead_itself_is_accepted(self) -> None:
        assert_schedulable_time(NOW + MIN_LEAD, NOW)

    def test_a_time_inside_the_minimum_lead_is_refused(self) -> None:
        with pytest.raises(ScheduleTimeError, match="2 minutes"):
            assert_schedulable_time(NOW + MIN_LEAD - timedelta(seconds=1), NOW)

    def test_a_past_time_is_refused(self) -> None:
        with pytest.raises(ScheduleTimeError):
            assert_schedulable_time(NOW - timedelta(minutes=5), NOW)

    def test_the_queue_horizon_itself_is_accepted(self) -> None:
        assert_schedulable_time(NOW + MAX_AHEAD, NOW)

    def test_a_time_past_the_queue_horizon_is_refused(self) -> None:
        """Cloud Tasks will not hold a task beyond 30 days."""
        with pytest.raises(ScheduleTimeError, match="29 days"):
            assert_schedulable_time(NOW + MAX_AHEAD + timedelta(seconds=1), NOW)

    def test_a_time_without_a_timezone_is_refused(self) -> None:
        """A naive time is ambiguous by five and a half hours in India."""
        with pytest.raises(ScheduleTimeError, match="timezone"):
            assert_schedulable_time(datetime(2026, 11, 4, 18, 0), NOW)

    def test_an_ist_time_is_compared_as_the_same_instant(self) -> None:
        """11:31 IST is 06:01 UTC: inside the minimum lead from 06:00 UTC."""
        with pytest.raises(ScheduleTimeError):
            assert_schedulable_time(datetime(2026, 11, 4, 11, 31, tzinfo=IST), NOW)
        assert_schedulable_time(datetime(2026, 11, 4, 11, 33, tzinfo=IST), NOW)


class TestDue:
    def test_the_moment_itself_is_due(self) -> None:
        assert is_due(NOW, NOW)

    def test_a_past_moment_is_due(self) -> None:
        assert is_due(NOW - timedelta(minutes=1), NOW)

    def test_a_future_moment_is_not_due(self) -> None:
        assert not is_due(NOW + timedelta(seconds=1), NOW)

    def test_a_missing_moment_is_never_due(self) -> None:
        """Starting it would send at a moment nobody chose."""
        assert not is_due(None, NOW)


class TestTooLate:
    LIMIT = timedelta(minutes=120)

    def test_on_time_is_not_too_late(self) -> None:
        assert not is_too_late(NOW, NOW, self.LIMIT)

    def test_exactly_at_the_limit_still_sends(self) -> None:
        assert not is_too_late(NOW - self.LIMIT, NOW, self.LIMIT)

    def test_past_the_limit_is_too_late(self) -> None:
        assert is_too_late(NOW - self.LIMIT - timedelta(seconds=1), NOW, self.LIMIT)
