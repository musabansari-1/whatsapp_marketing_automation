"""User queries.

Keeps SQL out of the route handlers so the HTTP layer stays about HTTP.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import UserRole
from app.persistence.models import User


async def get_by_id(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    return await session.get(User, user_id)


async def get_by_email(session: AsyncSession, email: str) -> User | None:
    """Look up by email, case-insensitively.

    Addresses are stored lowercased, but comparing case-insensitively means a user who
    types `Admin@Example.com` is not silently treated as a different account.
    """
    result = await session.execute(select(User).where(func.lower(User.email) == email.lower()))
    return result.scalar_one_or_none()


async def list_users(session: AsyncSession) -> Sequence[User]:
    result = await session.execute(select(User).order_by(User.created_at))
    return result.scalars().all()


async def list_active(session: AsyncSession) -> Sequence[User]:
    """Active users only — the [D3] booking-notification fan-out audience."""
    result = await session.execute(
        select(User).where(User.is_active.is_(True)).order_by(User.created_at)
    )
    return result.scalars().all()


async def admin_exists(session: AsyncSession) -> bool:
    """Whether any ADMIN account exists. The bootstrap command's guard ([D15])."""
    result = await session.execute(
        select(func.count()).select_from(User).where(User.role == UserRole.ADMIN)
    )
    return result.scalar_one() > 0


async def create(
    session: AsyncSession,
    *,
    email: str,
    password_hash: str,
    role: UserRole,
    full_name: str | None = None,
    is_active: bool = True,
) -> User:
    user = User(
        email=email.lower().strip(),
        password_hash=password_hash,
        role=role,
        full_name=full_name,
        is_active=is_active,
    )
    session.add(user)
    await session.flush()
    return user
