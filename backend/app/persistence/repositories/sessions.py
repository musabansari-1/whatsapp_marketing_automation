"""Session queries.

Sessions are looked up by the SHA-256 digest of the presented token; the raw token is
never stored, so a leaked database yields no usable sessions.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import delete, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession as DbSession
from sqlalchemy.orm import joinedload

from app.persistence.models import Session


async def create(
    session: DbSession,
    *,
    user_id: uuid.UUID,
    token_hash: str,
    expires_at: datetime,
) -> Session:
    record = Session(user_id=user_id, token_hash=token_hash, expires_at=expires_at)
    session.add(record)
    await session.flush()
    return record


async def get_by_token_hash(session: DbSession, token_hash: str) -> Session | None:
    """Fetch a session and its user in one round trip.

    Eager-loaded because every authenticated request needs the user, and a lazy load
    on an async session raises rather than silently working.
    """
    result = await session.execute(
        select(Session).options(joinedload(Session.user)).where(Session.token_hash == token_hash)
    )
    return result.scalar_one_or_none()


async def touch(session: DbSession, record: Session) -> None:
    """Record last use, for auditing which sessions are live."""
    record.last_used_at = datetime.now(UTC)
    await session.flush()


async def revoke(session: DbSession, record: Session) -> None:
    """End one session. This is what makes logout real rather than cosmetic."""
    record.revoked_at = datetime.now(UTC)
    await session.flush()


async def revoke_all_for_user(session: DbSession, user_id: uuid.UUID) -> int:
    """End every session for a user.

    Used when an account is deactivated: [D2]'s revocability is meaningless if a
    deactivated user keeps a live session until it expires.
    """
    result = await session.execute(
        update(Session)
        .where(Session.user_id == user_id, Session.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )
    await session.flush()
    # execute() is typed as Result; an UPDATE always yields a CursorResult carrying
    # rowcount.
    return int(cast("CursorResult[Any]", result).rowcount or 0)


async def delete_expired(session: DbSession, *, now: datetime | None = None) -> int:
    """Remove expired rows. Housekeeping; safe to run at any time."""
    result = await session.execute(
        delete(Session).where(Session.expires_at <= (now or datetime.now(UTC)))
    )
    await session.flush()
    return int(cast("CursorResult[Any]", result).rowcount or 0)
