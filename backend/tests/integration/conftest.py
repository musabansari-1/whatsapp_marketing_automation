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
import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

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


@pytest.fixture
def seeded_product(engine: AsyncEngine) -> uuid.UUID:
    """The product every existing row belongs to, as the migration seeds it (§28).

    The suite builds its schema with `create_all`, not by running migrations, so the
    row `d3f8b21c60ae` inserts is absent here. This fixture is its counterpart: from
    Phase 18 onwards, scoped tables require a product, and tests need one for the same
    reason a real database does.

    It deliberately does **not** reproduce the migration's slug or display name. Those
    are temporary configuration, and a test that only passes for one particular slug
    would make renaming the product a code change instead of a data change.
    """
    from app.persistence.models import Product

    async def ensure(session: AsyncSession) -> uuid.UUID:
        existing = (
            await session.execute(select(Product).order_by(Product.created_at).limit(1))
        ).scalar_one_or_none()
        if existing is not None:
            return existing.id

        product = Product(slug=f"seeded-{uuid.uuid4().hex[:8]}", display_name="Seeded product")
        session.add(product)
        await session.flush()
        return product.id

    from tests.integration.helpers import run_db

    return run_db(ensure)


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


# ---------------------------------------------------------------- API fixtures

ADMIN_EMAIL = "admin@example.com"
SALES_EMAIL = "sales@example.com"
PASSWORD = "a-sufficiently-long-password"  # noqa: S105 - test fixture, not a credential


@pytest.fixture(autouse=True)
def reset_rate_limits(engine: AsyncEngine) -> Iterator[None]:
    """Give every test its own rate limit allowance (§19).

    The counters are deliberately shared through the database — that is the whole point
    of the table — and the suite shares one database, one client address and one
    fifteen-minute window. Without this, the fortieth test to log in is refused because
    of the thirty-nine before it, and the failure looks like a bug in whatever that test
    was actually checking.

    Cleared rather than disabled, so a test cannot quietly come to depend on the limiter
    being off.

    Uses `run_db`, which builds its own engine: asyncpg connections are bound to the
    loop that created them, and borrowing the session-scoped engine here fails with
    "another operation is in progress".
    """
    from app.persistence.models import RateLimitCounter
    from tests.integration.helpers import run_db

    async def clear(db_session: AsyncSession) -> None:
        await db_session.execute(delete(RateLimitCounter))

    run_db(clear)
    yield


@pytest_asyncio.fixture
async def seeded_users(engine: AsyncEngine) -> AsyncIterator[dict[str, str]]:
    """One admin and one salesperson, removed afterwards.

    Committed rather than rolled back: the API under test opens its own sessions and
    would not see uncommitted rows.
    """
    from app.core.security import hash_password
    from app.domain.enums import UserRole
    from app.persistence.models import Session, User

    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as db_session:
        db_session.add_all(
            [
                User(
                    email=ADMIN_EMAIL,
                    password_hash=hash_password(PASSWORD),
                    role=UserRole.ADMIN,
                ),
                User(
                    email=SALES_EMAIL,
                    password_hash=hash_password(PASSWORD),
                    role=UserRole.SALESPERSON,
                ),
            ]
        )
        await db_session.commit()

    yield {"admin": ADMIN_EMAIL, "sales": SALES_EMAIL}

    async with factory() as db_session:
        await db_session.execute(delete(Session))
        await db_session.execute(delete(User))
        await db_session.commit()


@pytest.fixture
def client(engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """An app pointed at the test database.

    The app builds its own engine rather than borrowing the session-scoped one:
    TestClient runs the application on a separate event loop, and asyncpg connections
    are bound to the loop that created them. Sharing one engine across both fails with
    "another operation is in progress".

    Secure cookies are disabled because TestClient speaks http and a cookie jar will
    not return a Secure cookie over plaintext. This mirrors local http development;
    the Secure flag itself is asserted separately.

    Depending on `engine` still guarantees the schema exists and that the module skips
    when no database is reachable.
    """
    from app.api.main import create_app
    from app.core.config import Settings, reset_settings_cache
    from app.persistence import database

    monkeypatch.setenv("DATABASE_URL", _test_database_url())
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    reset_settings_cache()
    database._engine = None  # noqa: SLF001 - reset lazy singletons between tests
    database._session_factory = None  # noqa: SLF001

    # Exiting the context manager runs lifespan shutdown, disposing the engine on the
    # app's own loop.
    with TestClient(create_app(Settings())) as test_client:
        yield test_client

    database._engine = None  # noqa: SLF001
    database._session_factory = None  # noqa: SLF001
    reset_settings_cache()


def login(client: TestClient, email: str = ADMIN_EMAIL, password: str = PASSWORD) -> Response:
    """Log in through the API, returning the raw response."""
    # Annotated rather than returned directly: TestClient.post is untyped, so mypy
    # sees Any and rejects returning it from a typed function.
    response: Response = client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}
    )
    return response
