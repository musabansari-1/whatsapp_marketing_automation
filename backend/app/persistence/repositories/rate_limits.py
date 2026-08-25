"""Rate limit counting (§19).

One statement per check. `INSERT ... ON CONFLICT DO UPDATE ... RETURNING` increments and
reads in a single round trip, which is what makes the count correct under concurrency:
two workers cannot both read "one below the limit" and both proceed, because neither
reads a value it did not itself produce.

The decision about whether that count is allowed lives in `app.domain.rate_limit`; this
module only counts.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.rate_limit import (
    RateLimitDecision,
    RateLimitRule,
    RateLimitScope,
    bucket_key,
    decide,
    window_start,
)
from app.persistence.models import RateLimitCounter


async def hit(
    session: AsyncSession,
    *,
    scope: RateLimitScope,
    identifier: str,
    rule: RateLimitRule,
    now: datetime | None = None,
) -> RateLimitDecision:
    """Record one attempt and say whether it is allowed.

    The attempt is counted whether or not it is allowed. That is deliberate: an attacker
    who keeps trying past the limit should keep extending their own lockout rather than
    getting a fresh allowance the moment they pause.
    """
    moment = now or datetime.now(UTC)
    started_at = window_start(moment, rule.window_seconds)
    key = bucket_key(scope, identifier, window_start_at=started_at)

    statement = (
        pg_insert(RateLimitCounter)
        .values(
            bucket_key=key,
            count=1,
            expires_at=started_at + timedelta(seconds=rule.window_seconds),
        )
        .on_conflict_do_update(
            index_elements=[RateLimitCounter.bucket_key],
            set_={"count": RateLimitCounter.count + 1},
        )
        .returning(RateLimitCounter.count)
    )

    count = int((await session.execute(statement)).scalar_one())
    # Committed immediately: a rejected request raises, and an uncommitted increment
    # would be rolled back — handing the caller a free retry every time they are
    # refused.
    await session.commit()

    return decide(count=count, rule=rule, now=moment, window_start_at=started_at)


async def current_count(
    session: AsyncSession,
    *,
    scope: RateLimitScope,
    identifier: str,
    rule: RateLimitRule,
    now: datetime | None = None,
) -> int:
    """Read a counter without incrementing it. For tests and diagnostics."""
    moment = now or datetime.now(UTC)
    key = bucket_key(scope, identifier, window_start_at=window_start(moment, rule.window_seconds))
    result = await session.execute(
        select(RateLimitCounter.count).where(RateLimitCounter.bucket_key == key)
    )
    return int(result.scalars().first() or 0)


async def delete_expired(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Remove counters whose window has passed.

    They are already unreachable — the window start is part of the key — so this is
    housekeeping rather than correctness. Without it the table grows by one row per key
    per window forever.
    """
    moment = now or datetime.now(UTC)
    # RETURNING rather than rowcount: the typed Result exposes rows, and the count is
    # what makes the sweep's work visible in its log line.
    result = await session.execute(
        delete(RateLimitCounter)
        .where(RateLimitCounter.expires_at < moment)
        .returning(RateLimitCounter.id)
    )
    removed = len(list(result.scalars().all()))
    await session.flush()
    return removed


__all__ = ["current_count", "delete_expired", "hit"]
