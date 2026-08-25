"""Rate limit counters (§19).

NOTE — this table is not in the specification §15 list. §19 requires "appropriate rate
limits" without saying how they are stored; this is the implementation mechanism, and it
is reported as a deviation rather than introduced silently. `sessions` was added the same
way for [D2].

Why a table rather than in-process counters:

Cloud Run runs several instances. An in-process counter divides the effective limit by
the instance count, so a limit of five attempts becomes fifteen across three instances —
which is precisely the flaw already documented for client-side dispatch pacing. For a
security control the ceiling has to be absolute, so the count is shared.

Why not Redis: it would be one more piece of infrastructure to provision, secure and pay
for, and PostgreSQL already handles an atomic upsert-and-return perfectly well at the
volumes involved. If rate limiting ever becomes hot enough for that to matter, the
repository is the only thing that has to change.

One row per key per window. `bucket_key` carries the window start, so an expired window
is never read again — it is not reset, it is simply abandoned, and the rows are cleaned
up by a sweep.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.base import Base
from app.persistence.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class RateLimitCounter(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """How many attempts one key has made in one fixed window."""

    __tablename__ = "rate_limit_counters"

    # "<scope>:<identifier>:<window start epoch>" — unique, because the whole mechanism
    # is one atomic upsert against this column.
    bucket_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)

    # BigInteger rather than Integer: this is incremented by anyone who can reach the
    # endpoint, including whoever is being rate limited, so it should not be possible to
    # overflow it into an error.
    count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    # When this window ends. Used only by the cleanup sweep — the limiter itself never
    # reads it, because an expired window is unreachable by key.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_rate_limit_counters_expires_at", "expires_at"),)
