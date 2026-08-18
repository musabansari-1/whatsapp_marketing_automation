"""Helpers for integration tests that drive the API through a sync TestClient.

`TestClient` is synchronous, so a test using it cannot itself be async. When such a
test needs to change database state directly, it needs a way to run a coroutine.

`run_db` does that on its own event loop with its own short-lived engine. Reusing the
session-scoped engine here would fail: asyncpg connections are bound to the loop that
created them.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from typing import TypeVar

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

T = TypeVar("T")

DEFAULT_TEST_DATABASE_URL = (
    "postgresql+asyncpg://postgres:postgres@localhost:5442/whatsapp_marketing_test"
)


def get_test_database_url() -> str:
    return os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)


def run_db(operation: Callable[[AsyncSession], Awaitable[T]]) -> T:
    """Run one database operation in its own loop, committing on success."""

    async def _run() -> T:
        engine = create_async_engine(get_test_database_url())
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        try:
            async with factory() as session:
                result = await operation(session)
                await session.commit()
                return result
        finally:
            await engine.dispose()

    return asyncio.run(_run())
