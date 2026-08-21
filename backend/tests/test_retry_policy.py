"""Retry and backoff tests ([D27]).

Pure arithmetic, so the schedule is asserted exactly rather than observed by waiting.
"""

from __future__ import annotations

import random

import pytest

from app.core.errors import NonRetryableProviderError, RetryableProviderError
from app.domain.retry import (
    FailureClass,
    RetryPolicy,
    backoff_seconds,
    classify,
    is_retryable_error,
    should_retry,
)

NO_JITTER = RetryPolicy(max_attempts=5, base_seconds=2.0, max_seconds=300.0, jitter=0.0)


class TestPolicyDefaults:
    def test_defaults_match_the_decision(self) -> None:
        """[D27]: maximum 5 attempts."""
        assert RetryPolicy().max_attempts == 5

    @pytest.mark.parametrize(
        "kwargs",
        [{"max_attempts": 0}, {"base_seconds": 0}, {"jitter": 1.5}, {"jitter": -0.1}],
    )
    def test_invalid_policies_are_rejected(self, kwargs: dict[str, float]) -> None:
        with pytest.raises(ValueError):
            RetryPolicy(**kwargs)  # type: ignore[arg-type]


class TestFailureClassification:
    """[D27] requires the three classes to stay distinguishable."""

    def test_a_permanent_error_is_permanent(self) -> None:
        assert classify(NonRetryableProviderError("bad recipient")) is FailureClass.PERMANENT

    def test_a_transient_error_is_transient(self) -> None:
        assert classify(RetryableProviderError("connection reset")) is FailureClass.TRANSIENT

    def test_rate_limiting_is_reported_separately(self) -> None:
        """Both retry, but a campaign failing on rate limits means the dispatch rate
        is wrong rather than the provider being unwell — worth seeing distinctly."""
        assert classify(RetryableProviderError("Rate limited: slow down")) is (
            FailureClass.RATE_LIMITED
        )
        assert classify(RetryableProviderError("[130429] too many requests")) is (
            FailureClass.RATE_LIMITED
        )

    def test_an_unknown_error_is_treated_as_transient(self) -> None:
        """Giving up on something we do not understand risks abandoning a recoverable
        send; retrying costs an attempt and is bounded."""
        assert classify(RuntimeError("something odd")) is FailureClass.TRANSIENT

    def test_is_retryable_mirrors_the_taxonomy(self) -> None:
        assert is_retryable_error(RetryableProviderError("x")) is True
        assert is_retryable_error(NonRetryableProviderError("x")) is False
        assert is_retryable_error(RuntimeError("x")) is True


class TestShouldRetry:
    def test_a_permanent_failure_is_never_retried(self) -> None:
        """[D27]: retrying cannot help, and each attempt costs quota."""
        assert not should_retry(failure=FailureClass.PERMANENT, attempts_made=1, policy=NO_JITTER)

    @pytest.mark.parametrize("attempts", [1, 2, 3, 4])
    def test_transient_failures_retry_until_the_maximum(self, attempts: int) -> None:
        assert should_retry(
            failure=FailureClass.TRANSIENT, attempts_made=attempts, policy=NO_JITTER
        )

    def test_the_fifth_attempt_is_the_last(self) -> None:
        assert not should_retry(failure=FailureClass.TRANSIENT, attempts_made=5, policy=NO_JITTER)

    def test_rate_limits_retry_like_other_transients(self) -> None:
        assert should_retry(failure=FailureClass.RATE_LIMITED, attempts_made=1, policy=NO_JITTER)

    def test_the_maximum_is_configurable(self) -> None:
        policy = RetryPolicy(max_attempts=2)
        assert should_retry(failure=FailureClass.TRANSIENT, attempts_made=1, policy=policy)
        assert not should_retry(failure=FailureClass.TRANSIENT, attempts_made=2, policy=policy)


class TestBackoff:
    def test_the_delay_doubles(self) -> None:
        delays = [backoff_seconds(attempts_made=n, policy=NO_JITTER) for n in range(1, 6)]
        assert delays == [2.0, 4.0, 8.0, 16.0, 32.0]

    def test_the_delay_is_capped(self) -> None:
        """Without a cap, exponential growth reaches absurd delays within a dozen
        attempts."""
        policy = RetryPolicy(base_seconds=2.0, max_seconds=10.0, jitter=0.0)
        assert backoff_seconds(attempts_made=10, policy=policy) == 10.0

    def test_jitter_spreads_retries(self) -> None:
        """A broadcast fails as a batch — one rate limit usually means hundreds.
        Without jitter every task retries at the same instant and recreates the spike
        that caused the failure."""
        policy = RetryPolicy(base_seconds=10.0, jitter=0.5)
        delays = {
            backoff_seconds(attempts_made=1, policy=policy, rng=random.Random(seed))
            for seed in range(20)
        }
        assert len(delays) > 1

    def test_jitter_stays_within_bounds(self) -> None:
        policy = RetryPolicy(base_seconds=10.0, jitter=0.25)
        for seed in range(50):
            delay = backoff_seconds(attempts_made=1, policy=policy, rng=random.Random(seed))
            assert 7.5 <= delay <= 12.5

    def test_jitter_is_symmetric(self) -> None:
        """Centred on the nominal delay, so jitter spreads retries without
        systematically postponing them."""
        policy = RetryPolicy(base_seconds=10.0, jitter=0.5)
        delays = [
            backoff_seconds(attempts_made=1, policy=policy, rng=random.Random(seed))
            for seed in range(200)
        ]
        assert 9.0 < sum(delays) / len(delays) < 11.0

    def test_the_delay_is_never_negative(self) -> None:
        policy = RetryPolicy(base_seconds=1.0, jitter=1.0)
        for seed in range(50):
            assert backoff_seconds(attempts_made=1, policy=policy, rng=random.Random(seed)) >= 0

    def test_attempts_must_be_positive(self) -> None:
        with pytest.raises(ValueError):
            backoff_seconds(attempts_made=0, policy=NO_JITTER)
