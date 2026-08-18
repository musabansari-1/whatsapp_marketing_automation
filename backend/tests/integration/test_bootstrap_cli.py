"""First-ADMIN bootstrap tests ([D15]).

The guarantees that matter: it works once, it is safe to re-run, and it cannot be
used to slip an extra administrator past the API.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from typing import TypeVar

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.cli import EXIT_FAILED, EXIT_OK, bootstrap_admin
from app.core.config import Settings, reset_settings_cache
from app.core.security import hash_password, verify_password
from app.domain.enums import UserRole
from app.persistence import database
from app.persistence.models import Session, User
from tests.integration.helpers import get_test_database_url

pytestmark = pytest.mark.integration

T = TypeVar("T")

BOOTSTRAP_EMAIL = "founder@example.com"
BOOTSTRAP_PASSWORD = "the-first-admin-password"  # noqa: S105 - test fixture


@pytest_asyncio.fixture
async def db(engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A committing session factory on the shared test engine.

    These tests are async and run on the session event loop, so the CLI under test
    builds its own engine on that same loop. No cross-loop handoff is involved, which
    is why they can use the engine directly rather than the sync helper the
    TestClient-based tests need.
    """
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def clear() -> None:
        async with factory() as session:
            await session.execute(delete(Session))
            await session.execute(delete(User))
            await session.commit()

    await clear()
    yield factory
    await clear()


@pytest.fixture
def bootstrap_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point the CLI at the test database with bootstrap credentials configured."""
    monkeypatch.setenv("DATABASE_URL", get_test_database_url())
    monkeypatch.setenv("BOOTSTRAP_ADMIN_EMAIL", BOOTSTRAP_EMAIL)
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", BOOTSTRAP_PASSWORD)
    reset_settings_cache()
    database._engine = None  # noqa: SLF001
    database._session_factory = None  # noqa: SLF001
    yield
    database._engine = None  # noqa: SLF001
    database._session_factory = None  # noqa: SLF001
    reset_settings_cache()


async def _count_users(session: AsyncSession) -> int:
    return int((await session.execute(select(func.count()).select_from(User))).scalar_one())


async def _get_admin(session: AsyncSession) -> User | None:
    result = await session.execute(select(User).where(User.email == BOOTSTRAP_EMAIL))
    return result.scalar_one_or_none()


async def _with(
    factory: async_sessionmaker[AsyncSession],
    operation: Callable[[AsyncSession], Awaitable[T]],
    *,
    commit: bool = False,
) -> T:
    """Run one operation on a fresh session from `factory`."""
    async with factory() as session:
        result = await operation(session)
        if commit:
            await session.commit()
        return result


class TestBootstrap:
    async def test_creates_the_first_admin(
        self, bootstrap_env: None, db: async_sessionmaker[AsyncSession]
    ) -> None:
        assert await bootstrap_admin(Settings()) == EXIT_OK

        admin = await _with(db, _get_admin)
        assert admin is not None
        assert admin.role is UserRole.ADMIN
        assert admin.is_active is True

    async def test_password_is_hashed_not_stored(
        self, bootstrap_env: None, db: async_sessionmaker[AsyncSession]
    ) -> None:
        await bootstrap_admin(Settings())

        admin = await _with(db, _get_admin)
        assert admin is not None
        assert BOOTSTRAP_PASSWORD not in admin.password_hash
        assert admin.password_hash.startswith("$argon2id$")
        assert verify_password(BOOTSTRAP_PASSWORD, admin.password_hash) is True

    async def test_rerunning_is_idempotent(
        self, bootstrap_env: None, db: async_sessionmaker[AsyncSession]
    ) -> None:
        """Safe to run on every deploy: the second run changes nothing."""
        assert await bootstrap_admin(Settings()) == EXIT_OK
        assert await bootstrap_admin(Settings()) == EXIT_OK
        assert await _with(db, _count_users) == 1

    async def test_refuses_when_another_admin_exists(
        self, bootstrap_env: None, db: async_sessionmaker[AsyncSession]
    ) -> None:
        """[D15]: it cannot be used to add a second administrator.

        Otherwise anyone able to set environment variables could grant themselves
        admin access without going through the API's authorization.
        """

        async def seed_other_admin(session: AsyncSession) -> None:
            session.add(
                User(
                    email="someone.else@example.com",
                    password_hash=hash_password("x" * 16),
                    role=UserRole.ADMIN,
                )
            )

        await _with(db, seed_other_admin, commit=True)

        assert await bootstrap_admin(Settings()) == EXIT_OK
        assert await _with(db, _get_admin) is None  # our bootstrap user was not created
        assert await _with(db, _count_users) == 1

    async def test_a_salesperson_does_not_block_bootstrap(
        self, bootstrap_env: None, db: async_sessionmaker[AsyncSession]
    ) -> None:
        """The guard is specifically about administrators."""

        async def seed_sales(session: AsyncSession) -> None:
            session.add(
                User(
                    email="sales.only@example.com",
                    password_hash=hash_password("x" * 16),
                    role=UserRole.SALESPERSON,
                )
            )

        await _with(db, seed_sales, commit=True)

        assert await bootstrap_admin(Settings()) == EXIT_OK
        assert await _with(db, _get_admin) is not None

    async def test_fails_without_configuration(
        self,
        bootstrap_env: None,
        db: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("BOOTSTRAP_ADMIN_PASSWORD", raising=False)
        reset_settings_cache()

        assert await bootstrap_admin(Settings()) == EXIT_FAILED
        assert await _with(db, _count_users) == 0

    async def test_refuses_when_email_taken_by_a_salesperson(
        self, bootstrap_env: None, db: async_sessionmaker[AsyncSession]
    ) -> None:
        """Reusing an existing address must not silently promote that account."""

        async def seed_same_email(session: AsyncSession) -> None:
            session.add(
                User(
                    email=BOOTSTRAP_EMAIL,
                    password_hash=hash_password("x" * 16),
                    role=UserRole.SALESPERSON,
                )
            )

        await _with(db, seed_same_email, commit=True)

        assert await bootstrap_admin(Settings()) == EXIT_FAILED

        admin = await _with(db, _get_admin)
        assert admin is not None
        assert admin.role is UserRole.SALESPERSON  # unchanged
