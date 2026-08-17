"""Fixtures for tests that need real PostgreSQL.

A live database with pgvector is required, because the behaviour under test —
constraint enforcement, enum round-tripping, vector similarity — cannot be verified
against SQLAlchemy metadata or SQLite.

When no database is reachable these tests skip rather than fail, so `pytest` stays
green on a machine without Docker running. CI must run them; Phase 15 sets that up.

The schema is created once per session from the models, and each test runs inside a
transaction that is rolled back, so tests cannot see each other's rows.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, create_async_engine

from app.persistence.base import Base

# Registers every model on Base.metadata before create_all runs.
import app.persistence.models  # noqa: F401  # isort: skip

DEFAULT_TEST_DATABASE_URL = (
    "postgresql+asyncpg://postgres:postgres@localhost:5442/whatsapp_marketing_test"
)


def _test_database_url() -> str:
    return os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)


@pytest_asyncio.fixture(scope="session")
async def engine() -> AsyncIterator[AsyncEngine]:
    """Session-scoped engine, or skip the whole module if unreachable."""
    engine = create_async_engine(_test_database_url(), poolclass=None)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - any connection failure means skip
        await engine.dispose()
        pytest.skip(
            f"PostgreSQL not reachable at {_test_database_url()}: {type(exc).__name__}. "
            "Start it with `docker compose up -d postgres` and create the test database.",
            allow_module_level=True,
        )

    async with engine.begin() as connection:
        await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)

    yield engine

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def connection(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """A connection in a transaction that is always rolled back."""
    async with engine.connect() as conn:
        transaction = await conn.begin()
        try:
            yield conn
        finally:
            # A test that asserted on an IntegrityError has already aborted the
            # transaction, so rolling back again would warn.
            if transaction.is_active:
                await transaction.rollback()


@pytest_asyncio.fixture
async def session(connection: AsyncConnection) -> AsyncIterator[AsyncSession]:
    """A session bound to the rolled-back transaction."""
    async with AsyncSession(bind=connection, expire_on_commit=False) as db_session:
        yield db_session
