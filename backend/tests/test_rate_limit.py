"""Rate limit policy tests (§19, D-42, D-45).

Pure policy: no database, no HTTP. The window arithmetic is where this gets subtly wrong,
so it is asserted directly rather than only through the endpoints.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.domain.rate_limit import (
    RateLimitRule,
    RateLimitScope,
    account_identifier,
    bucket_key,
    client_identifier,
    decide,
    window_start,
)

NOW = datetime(2026, 9, 2, 10, 17, 33, tzinfo=UTC)


class TestRuleValidation:
    def test_a_valid_rule_is_accepted(self) -> None:
        assert RateLimitRule(limit=5, window_seconds=60).limit == 5

    @pytest.mark.parametrize("limit", [0, -1])
    def test_a_rule_must_allow_at_least_one_attempt(self, limit: int) -> None:
        """A limit of zero would refuse everyone including legitimate users, which is
        an outage rather than a rate limit."""
        with pytest.raises(ValueError, match="at least one"):
            RateLimitRule(limit=limit, window_seconds=60)

    @pytest.mark.parametrize("window", [0, -60])
    def test_the_window_must_be_positive(self, window: int) -> None:
        with pytest.raises(ValueError, match="positive"):
            RateLimitRule(limit=5, window_seconds=window)


class TestWindowArithmetic:
    def test_the_window_is_truncated_against_the_epoch(self) -> None:
        """Against the epoch, not process start — so every Cloud Run instance computes
        the same boundary for the same instant, which is what makes the counter shared
        rather than per-instance."""
        started = window_start(NOW, 900)

        assert started <= NOW
        assert int(started.timestamp()) % 900 == 0

    def test_two_instants_in_one_window_share_a_start(self) -> None:
        first = window_start(NOW, 900)
        second = window_start(NOW + timedelta(seconds=60), 900)

        assert first == second

    def test_instants_in_different_windows_differ(self) -> None:
        first = window_start(NOW, 60)
        second = window_start(NOW + timedelta(seconds=120), 60)

        assert first != second

    def test_the_window_start_is_timezone_aware(self) -> None:
        assert window_start(NOW, 60).tzinfo is UTC


class TestBucketKeys:
    def test_the_key_carries_scope_identifier_and_window(self) -> None:
        key = bucket_key(RateLimitScope.LOGIN_IP, "1.2.3.4", window_start_at=window_start(NOW, 900))

        assert key.startswith("login_ip:1.2.3.4:")

    def test_scopes_do_not_share_a_budget(self) -> None:
        """A flood of failed logins must not consume the webhook's allowance."""
        started = window_start(NOW, 900)

        assert bucket_key(RateLimitScope.LOGIN_IP, "x", window_start_at=started) != bucket_key(
            RateLimitScope.WEBHOOK_IP, "x", window_start_at=started
        )

    def test_a_new_window_is_a_new_key(self) -> None:
        """Which is what makes an expired window unreachable rather than needing a
        reset."""
        first = bucket_key(
            RateLimitScope.LOGIN_IP, "1.2.3.4", window_start_at=window_start(NOW, 60)
        )
        later = bucket_key(
            RateLimitScope.LOGIN_IP,
            "1.2.3.4",
            window_start_at=window_start(NOW + timedelta(seconds=120), 60),
        )

        assert first != later


class TestDecisions:
    def test_an_attempt_within_the_limit_is_allowed(self) -> None:
        rule = RateLimitRule(limit=5, window_seconds=900)
        decision = decide(count=3, rule=rule, now=NOW, window_start_at=window_start(NOW, 900))

        assert decision.allowed
        assert decision.remaining == 2

    def test_the_limiting_attempt_is_still_allowed(self) -> None:
        """`count` includes the current attempt, so reaching exactly the limit is the
        last permitted one rather than the first refused."""
        rule = RateLimitRule(limit=5, window_seconds=900)
        decision = decide(count=5, rule=rule, now=NOW, window_start_at=window_start(NOW, 900))

        assert decision.allowed
        assert decision.remaining == 0

    def test_one_past_the_limit_is_refused(self) -> None:
        rule = RateLimitRule(limit=5, window_seconds=900)
        decision = decide(count=6, rule=rule, now=NOW, window_start_at=window_start(NOW, 900))

        assert not decision.allowed

    def test_retry_after_points_at_the_window_end(self) -> None:
        """The caller is told when to come back, not a guess."""
        rule = RateLimitRule(limit=1, window_seconds=900)
        started = window_start(NOW, 900)
        decision = decide(count=9, rule=rule, now=NOW, window_start_at=started)

        expected = int((started + timedelta(seconds=900) - NOW).total_seconds())
        assert decision.retry_after_seconds == expected

    def test_retry_after_is_never_zero(self) -> None:
        """A `Retry-After: 0` invites an immediate retry, which is the opposite of the
        point."""
        rule = RateLimitRule(limit=1, window_seconds=60)
        started = window_start(NOW, 60)
        at_the_edge = started + timedelta(seconds=60)

        decision = decide(count=2, rule=rule, now=at_the_edge, window_start_at=started)
        assert decision.retry_after_seconds >= 1

    def test_remaining_never_goes_negative(self) -> None:
        rule = RateLimitRule(limit=2, window_seconds=60)
        decision = decide(count=99, rule=rule, now=NOW, window_start_at=window_start(NOW, 60))

        assert decision.remaining == 0


class TestIdentifiers:
    def test_an_account_key_is_case_insensitive(self) -> None:
        """An attacker varying capitalisation would otherwise get a fresh allowance per
        spelling."""
        assert account_identifier("Asha@Example.com") == account_identifier("asha@example.com")

    def test_an_account_key_is_trimmed(self) -> None:
        assert account_identifier("  asha@example.com  ") == "asha@example.com"

    def test_the_forwarded_header_wins_over_the_socket(self) -> None:
        """Behind Cloud Run the socket address is the load balancer, so every caller
        would otherwise share one budget."""
        assert client_identifier("203.0.113.7", "10.0.0.1") == "203.0.113.7"

    def test_the_first_forwarded_entry_is_the_client(self) -> None:
        """Later entries are proxies; the original client is first."""
        assert client_identifier("203.0.113.7, 10.0.0.1, 10.0.0.2", None) == "203.0.113.7"

    def test_the_socket_is_used_when_there_is_no_header(self) -> None:
        assert client_identifier(None, "10.0.0.1") == "10.0.0.1"

    def test_an_unidentifiable_caller_shares_one_budget(self) -> None:
        """Rather than each getting an unlimited one."""
        assert client_identifier(None, None) == "unknown"
        assert client_identifier("  ", None) == "unknown"

    def test_a_long_header_is_truncated(self) -> None:
        """The header is client-controlled, so its length is too. An unbounded value
        would become an unbounded database key."""
        assert len(client_identifier("x" * 5000, None)) <= 100
