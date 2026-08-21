"""Retry and backoff policy ([D27]).

Pure arithmetic, so the schedule can be asserted exactly rather than observed by
waiting. Nothing here performs a delay; it computes how long the caller should wait.

§20 and [D27] require three failure classes to stay distinguishable, because each
demands different behaviour: transient failures retry, permanent failures never do,
and a rate-limit response is a transient failure the provider has already told us how
to handle.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from enum import StrEnum

from app.core.errors import ProviderError, RetryableProviderError


class FailureClass(StrEnum):
    """[D27]'s three classes."""

    TRANSIENT = "transient"
    PERMANENT = "permanent"
    RATE_LIMITED = "rate_limited"


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """How many attempts, and how long to wait between them."""

    max_attempts: int = 5
    base_seconds: float = 2.0
    max_seconds: float = 300.0
    jitter: float = 0.25

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.base_seconds <= 0:
            raise ValueError("base_seconds must be positive")
        if not 0.0 <= self.jitter <= 1.0:
            raise ValueError("jitter must be between 0 and 1")


def classify(error: Exception) -> FailureClass:
    """Sort a provider failure into its class.

    Rate limiting is reported separately from other transient failures even though
    both retry, because it is worth seeing in logs and metrics: a campaign failing on
    rate limits means the dispatch rate is wrong, not that the provider is unwell.
    """
    if isinstance(error, ProviderError):
        if not error.retryable:
            return FailureClass.PERMANENT
        message = str(error).lower()
        if "rate limit" in message or "130429" in message or "too many" in message:
            return FailureClass.RATE_LIMITED
        return FailureClass.TRANSIENT

    # An unrecognised error is treated as transient. Giving up on something we do not
    # understand risks abandoning a recoverable send; retrying it costs an attempt and
    # is bounded by max_attempts.
    return FailureClass.TRANSIENT


def should_retry(*, failure: FailureClass, attempts_made: int, policy: RetryPolicy) -> bool:
    """Whether another attempt is permitted.

    `attempts_made` counts attempts already completed, including the one that just
    failed.
    """
    if failure is FailureClass.PERMANENT:
        return False
    return attempts_made < policy.max_attempts


def backoff_seconds(
    *, attempts_made: int, policy: RetryPolicy, rng: random.Random | None = None
) -> float:
    """Delay before the next attempt: exponential, jittered, and capped.

    Jitter matters more than it looks. A broadcast fails as a batch — one rate limit
    usually means hundreds — and without jitter every task would retry at the same
    instant and recreate the spike that caused the failure.
    """
    if attempts_made < 1:
        raise ValueError("attempts_made must be at least 1")

    delay: float = min(policy.base_seconds * (2 ** (attempts_made - 1)), policy.max_seconds)

    if policy.jitter:
        # A concrete Random rather than the module, so the type is exact and a test
        # can pass a seeded generator for a deterministic schedule.
        # noqa S311: retry jitter is scheduling noise, not a security control.
        source = rng if rng is not None else random.Random()  # noqa: S311
        # Symmetric around the nominal delay, so jitter spreads retries without
        # systematically postponing them.
        spread = delay * policy.jitter
        delay += source.uniform(-spread, spread)

    return max(0.0, min(delay, policy.max_seconds))


def is_retryable_error(error: Exception) -> bool:
    """Convenience predicate mirroring the §20 taxonomy."""
    return isinstance(error, RetryableProviderError) or not isinstance(error, ProviderError)
