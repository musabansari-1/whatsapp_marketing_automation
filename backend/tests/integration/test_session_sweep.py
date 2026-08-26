"""Expired session cleanup (D-43).

`sessions.delete_expired` has existed since Phase 3 with no caller, so expired rows
accumulated for the life of a deployment. These prove the sweep is that caller, and —
more importantly — that it deletes only what is genuinely unreachable. A sweep that took
a live session with it would log every user out.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import UserRole
from app.integrations.queue import TaskRequest, TaskType
from app.persistence.models import Session, User
from app.worker.handlers.sweeps import sweep_expired_sessions
from tests.integration.helpers import get_test_database_url, run_db

pytestmark = pytest.mark.integration


@pytest.fixture
def session_env(engine: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A clean sessions/users pair, and the handler pointed at the test database."""
    from app.core.config import reset_settings_cache
    from app.persistence import database

    monkeypatch.setenv("DATABASE_URL", get_test_database_url())
    reset_settings_cache()
    database._engine = None  # noqa: SLF001
    database._session_factory = None  # noqa: SLF001

    async def clear(session: AsyncSession) -> None:
        await session.execute(delete(Session))
        await session.execute(delete(User))

    run_db(clear)
    yield
    run_db(clear)


def seed_user() -> uuid.UUID:
    async def seed(session: AsyncSession) -> uuid.UUID:
        user = User(
            email=f"sweep-{uuid.uuid4()}@example.test",
            # Not a real hash and never verified here; the column is only NOT NULL.
            password_hash="x",
            role=UserRole.ADMIN,
            is_active=True,
        )
        session.add(user)
        await session.flush()
        return user.id

    return run_db(seed)


def seed_session(user_id: uuid.UUID, *, expires_in: timedelta, revoked: bool = False) -> uuid.UUID:
    """One session row, expiring relative to now."""

    async def seed(session: AsyncSession) -> uuid.UUID:
        token = uuid.uuid4().hex
        row = Session(
            user_id=user_id,
            token_hash=hashlib.sha256(token.encode()).hexdigest(),
            expires_at=datetime.now(UTC) + expires_in,
            revoked_at=datetime.now(UTC) if revoked else None,
        )
        session.add(row)
        await session.flush()
        return row.id

    return run_db(seed)


def surviving_ids() -> set[uuid.UUID]:
    async def read(session: AsyncSession) -> set[uuid.UUID]:
        result = await session.execute(select(Session.id))
        return set(result.scalars().all())

    return run_db(read)


def run_sweep() -> dict[str, Any]:
    from app.persistence import database

    async def run() -> dict[str, Any]:
        database._engine = None  # noqa: SLF001
        database._session_factory = None  # noqa: SLF001
        try:
            result = await sweep_expired_sessions(
                TaskRequest(
                    task_type=TaskType.SWEEP_EXPIRED_SESSIONS,
                    task_id="sweep-sessions-1",
                    payload={},
                )
            )
            return dict(result)
        finally:
            await database.dispose_engine()

    return asyncio.run(run())


class TestSessionSweep:
    def test_an_expired_session_is_removed(self, session_env: None) -> None:
        user_id = seed_user()
        expired = seed_session(user_id, expires_in=timedelta(hours=-1))

        result = run_sweep()

        assert result["removed"] == 1
        assert expired not in surviving_ids()

    def test_a_live_session_is_left_alone(self, session_env: None) -> None:
        """The failure that would matter: sweeping a session still in use logs a user out."""
        user_id = seed_user()
        live = seed_session(user_id, expires_in=timedelta(hours=12))
        seed_session(user_id, expires_in=timedelta(minutes=-1))

        result = run_sweep()

        assert result["removed"] == 1
        assert surviving_ids() == {live}

    def test_a_revoked_but_unexpired_session_is_left_alone(self, session_env: None) -> None:
        """Revocation is enforced on read. Deleting the row early would only lose the trail.

        Stated as a test rather than left implicit, because "revoked" reads like "finished"
        and a future change could reasonably decide to sweep it. That would be a different
        decision, not a tidy-up.
        """
        user_id = seed_user()
        revoked = seed_session(user_id, expires_in=timedelta(hours=6), revoked=True)

        result = run_sweep()

        assert result["removed"] == 0
        assert surviving_ids() == {revoked}

    def test_an_empty_sweep_is_not_an_error(self, session_env: None) -> None:
        result = run_sweep()

        assert result["result"] == "swept"
        assert result["removed"] == 0

    def test_sweeping_twice_removes_nothing_the_second_time(self, session_env: None) -> None:
        """Idempotent, because a scheduled job runs whether or not there is work."""
        user_id = seed_user()
        seed_session(user_id, expires_in=timedelta(days=-2))

        assert run_sweep()["removed"] == 1
        assert run_sweep()["removed"] == 0
