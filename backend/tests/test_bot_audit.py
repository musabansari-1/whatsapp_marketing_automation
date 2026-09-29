"""Bot-activity scoring tests ([D67]).

Pure domain, so every leg of the rule is stated exactly: the staging marathon
flags, ordinary conversations do not, and each boundary is pinned. No database,
no clock beyond explicit timestamps.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.domain.bot_audit import (
    BotSignals,
    BotThresholds,
    judge,
    median_gap_seconds,
)

THRESHOLDS = BotThresholds(msgs_per_hour=30, max_median_gap_seconds=120, min_lifetime_messages=100)

T0 = datetime(2026, 9, 26, 4, 0, tzinfo=UTC)


def every(seconds: int, count: int) -> tuple[datetime, ...]:
    return tuple(T0 + n * timedelta(seconds=seconds) for n in range(count))


class TestMedianGap:
    def test_needs_two_messages(self) -> None:
        assert median_gap_seconds(()) is None
        assert median_gap_seconds((T0,)) is None

    def test_odd_count_takes_the_middle(self) -> None:
        assert median_gap_seconds(every(10, 4)) == 10.0

    def test_even_count_averages_the_middle_two(self) -> None:
        assert median_gap_seconds(every(10, 5)) == 10.0
        starts = (T0, T0 + timedelta(seconds=10), T0 + timedelta(seconds=30))
        assert median_gap_seconds(starts) == 15.0

    def test_unsorted_input_is_sorted(self) -> None:
        starts = (T0 + timedelta(seconds=20), T0, T0 + timedelta(seconds=10))
        assert median_gap_seconds(starts) == 10.0


class TestJudge:
    def test_the_staging_marathon_flags(self) -> None:
        """44/hour at a 12-second median gap across a 900-message history."""
        verdict = judge(
            BotSignals(
                conversation_id="c",
                window_messages=44,
                window_starts=every(12, 44),
                lifetime_messages=900,
            ),
            THRESHOLDS,
        )

        assert verdict.suspect is True
        assert len(verdict.reasons) == 3

    def test_an_ordinary_chat_is_clean(self) -> None:
        verdict = judge(
            BotSignals(
                conversation_id="c",
                window_messages=6,
                window_starts=every(300, 6),
                lifetime_messages=14,
            ),
            THRESHOLDS,
        )

        assert verdict.suspect is False
        assert verdict.reasons == ()

    def test_a_short_burst_below_the_rate_is_clean(self) -> None:
        """Twenty rapid human replies are excitement, not automation: pace gates."""
        verdict = judge(
            BotSignals(
                conversation_id="c",
                window_messages=20,
                window_starts=every(15, 20),
                lifetime_messages=20,
            ),
            THRESHOLDS,
        )

        assert verdict.suspect is False

    def test_steady_but_human_paced_traffic_is_clean(self) -> None:
        """Thirty messages three minutes apart is a busy human day, not a loop:
        without machine regularity or marathon history, pace alone never flags."""
        verdict = judge(
            BotSignals(
                conversation_id="c",
                window_messages=30,
                window_starts=every(180, 30),
                lifetime_messages=30,
            ),
            THRESHOLDS,
        )

        assert verdict.suspect is False

    def test_boundaries_belong_to_the_flag(self) -> None:
        verdict = judge(
            BotSignals(
                conversation_id="c",
                window_messages=30,
                window_starts=every(120, 30),
                lifetime_messages=100,
            ),
            THRESHOLDS,
        )

        assert verdict.suspect is True

    def test_a_fresh_bot_flags_on_regularity_before_the_marathon(self) -> None:
        """A new attacker must not get a free first hundred messages: sustained
        pace with machine gaps flags on shape alone."""
        verdict = judge(
            BotSignals(
                conversation_id="c",
                window_messages=35,
                window_starts=every(45, 35),
                lifetime_messages=35,
            ),
            THRESHOLDS,
        )

        assert verdict.suspect is True
        assert len(verdict.reasons) == 2
