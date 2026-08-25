"""Rate limiting policy (specification §19).

§19 requires "appropriate rate limits". This module decides what that means; the
persistence layer counts and the API layer enforces.

**Fixed windows, not a sliding log.** A sliding window is more accurate and needs a row
per attempt; a fixed window needs one counter per key per window. The cost of the choice
is a boundary burst — an attacker timing requests either side of a window edge gets up to
twice the limit in quick succession — which is acceptable for the thing this protects
against, and is stated here rather than discovered later.

**Counted in the database, not in the process.** Cloud Run runs several instances, so an
in-process counter would divide the effective limit by the instance count. That is
exactly the flaw already documented for client-side dispatch pacing, and it would be
worse here because the whole point is an absolute ceiling.

Every threshold is configuration. §19 asks for appropriate limits and does not say what
they are; picking numbers in code would make them a deployment problem to change.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum


class RateLimitScope(StrEnum):
    """What is being limited.

    Separate scopes so one cannot exhaust another: a flood of failed logins from one
    address must not consume the webhook's budget.
    """

    LOGIN_IP = "login_ip"
    LOGIN_ACCOUNT = "login_account"
    WEBHOOK_IP = "webhook_ip"
    UPLOAD_USER = "upload_user"


@dataclass(frozen=True, slots=True)
class RateLimitRule:
    """How many attempts are allowed in how long."""

    limit: int
    window_seconds: int

    def __post_init__(self) -> None:
        if self.limit <= 0:
            raise ValueError("A rate limit must allow at least one attempt")
        if self.window_seconds <= 0:
            raise ValueError("A rate limit window must be positive")


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    """The outcome of one check."""

    allowed: bool
    limit: int
    current: int
    retry_after_seconds: int

    @property
    def remaining(self) -> int:
        return max(self.limit - self.current, 0)


def window_start(moment: datetime, window_seconds: int) -> datetime:
    """The start of the fixed window `moment` falls in.

    Truncated against the epoch rather than against process start, so every instance
    computes the same boundary for the same instant — which is what makes the counter
    shared rather than per-instance.
    """
    epoch_seconds = int(moment.timestamp())
    return datetime.fromtimestamp(epoch_seconds - (epoch_seconds % window_seconds), tz=UTC)


def bucket_key(scope: RateLimitScope, identifier: str, *, window_start_at: datetime) -> str:
    """The counter this attempt increments.

    The window start is part of the key, so an expired window is simply never read
    again rather than needing to be reset.
    """
    return f"{scope.value}:{identifier}:{int(window_start_at.timestamp())}"


def decide(
    *, count: int, rule: RateLimitRule, now: datetime, window_start_at: datetime
) -> RateLimitDecision:
    """Whether this attempt is allowed, given the count *including* it.

    `count` includes the current attempt because the counter is incremented and read in
    one statement — two workers must not both read "one below the limit" and both
    proceed.
    """
    allowed = count <= rule.limit
    window_end = window_start_at + timedelta(seconds=rule.window_seconds)
    retry_after = max(int((window_end - now).total_seconds()), 1)

    return RateLimitDecision(
        allowed=allowed,
        limit=rule.limit,
        current=count,
        retry_after_seconds=retry_after,
    )


def account_identifier(email: str) -> str:
    """The per-account key.

    Lower-cased so `Asha@example.com` and `asha@example.com` share a budget; an attacker
    varying capitalisation would otherwise get a fresh allowance per spelling.
    """
    return email.strip().lower()


def client_identifier(forwarded_for: str | None, client_host: str | None) -> str:
    """The caller's address, as well as it can be known.

    Behind Cloud Run the socket address is the load balancer, so `X-Forwarded-For` is
    what identifies the caller. Its **first** entry is the original client; later ones
    are proxies. The header is client-controlled and therefore spoofable — which is why
    the per-account limit exists alongside the per-IP one, and why neither is relied on
    alone.

    Falls back to a constant when nothing is available, so an unidentifiable caller
    shares one budget rather than getting an unlimited one each.
    """
    if forwarded_for:
        first = forwarded_for.split(",")[0].strip()
        if first:
            return first[:100]
    return (client_host or "unknown")[:100]


__all__ = [
    "RateLimitDecision",
    "RateLimitRule",
    "RateLimitScope",
    "account_identifier",
    "bucket_key",
    "client_identifier",
    "decide",
    "window_start",
]
