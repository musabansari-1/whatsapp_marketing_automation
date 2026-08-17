"""Database engine and session management.

Phase 1 provides connectivity only. Tables, enums, indexes and constraints are
Phase 2; no ORM models exist yet.

Neon note (recorded as a Phase 0 risk): the application uses the pooled connection
endpoint while Alembic migrations must run against the direct endpoint. Both come
from configuration, so nothing here needs to change when that split is applied.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings, get_settings
from app.persistence.base import Base

__all__ = ["Base", "check_database_connection", "dispose_engine", "get_engine", "get_session"]

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine(settings: Settings | None = None) -> AsyncEngine:
    """Return the process-wide async engine, creating it on first use.

    Created lazily rather than at import time so importing the app does not require
    a reachable database (tests, CLI commands, and Cloud Run cold starts).
    """
    global _engine
    if _engine is None:
        settings = settings or get_settings()
        _engine = create_async_engine(
            settings.database_url,
            echo=settings.database_echo,
            pool_size=settings.database_pool_size,
            max_overflow=settings.database_max_overflow,
            pool_pre_ping=True,
        )
    return _engine


def get_session_factory(settings: Settings | None = None) -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(settings),
            expire_on_commit=False,
            autoflush=False,
        )
    return _session_factory


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a session and rolling back on error."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def check_database_connection(settings: Settings | None = None) -> bool:
    """Readiness probe. Returns False rather than raising so the caller can report."""
    try:
        engine = get_engine(settings)
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 - probe reports failure, never propagates
        return False
    return True


async def dispose_engine() -> None:
    """Release pooled connections on shutdown and reset the lazy singletons."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
