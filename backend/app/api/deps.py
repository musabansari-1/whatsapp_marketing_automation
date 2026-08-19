"""FastAPI dependencies.

The single enforcement point for authentication and authorization. Route handlers
declare what they need — a user, or an admin — and never re-implement the checks.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.correlation import bind_log_context
from app.core.security import hash_session_token
from app.domain.auth import SessionState, can_manage_users, is_session_valid
from app.integrations.queue import QueueProvider
from app.integrations.registry import build_queue_provider
from app.persistence.database import get_session_factory
from app.persistence.models import User
from app.persistence.repositories import sessions as session_repo

# Deliberately identical for every authentication failure — missing cookie, unknown
# token, expired session, revoked session, deactivated user. Distinguishing them would
# tell an attacker which of those they achieved.
_UNAUTHENTICATED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated",
)


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    """A request-scoped database session, committed on success.

    Committing here rather than in each handler means a handler that raises leaves
    nothing half-written.
    """
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


DbSession = Annotated[AsyncSession, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


async def get_current_user(
    request: Request,
    session: DbSession,
    settings: SettingsDep,
) -> User:
    """Resolve the caller from the session cookie, or reject.

    The cookie carries an opaque token; the stored row holds only its digest, so the
    lookup hashes what was presented rather than comparing secrets.
    """
    token = request.cookies.get(settings.session_cookie_name)
    if not token:
        raise _UNAUTHENTICATED

    record = await session_repo.get_by_token_hash(session, hash_session_token(token))
    if record is None:
        raise _UNAUTHENTICATED

    state = SessionState(
        expires_at=record.expires_at,
        revoked_at=record.revoked_at,
        user_is_active=record.user.is_active,
    )
    if not is_session_valid(state):
        raise _UNAUTHENTICATED

    await session_repo.touch(session, record)

    # Every subsequent log line in this request identifies the actor (§21).
    bind_log_context(user_id=str(record.user_id))
    return record.user


CurrentUser = Annotated[User, Depends(get_current_user)]


async def require_admin(user: CurrentUser) -> User:
    """Admin-only routes ([D14]).

    403 rather than 401: the caller is authenticated, just not permitted, and
    conflating the two would send a logged-in salesperson to the login page.
    """
    if not can_manage_users(user.role):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator role required",
        )
    return user


AdminUser = Annotated[User, Depends(require_admin)]


# Built once per process rather than per request, so the in-memory implementation
# used before Phase 7 retains what was enqueued and tests can inspect it. Reset via
# `reset_queue_provider` the same way the database singletons are.
_queue_provider: QueueProvider | None = None


def get_queue_provider(settings: SettingsDep) -> QueueProvider:
    global _queue_provider
    if _queue_provider is None:
        _queue_provider = build_queue_provider(settings)
    return _queue_provider


def reset_queue_provider() -> None:
    global _queue_provider
    _queue_provider = None


Queue = Annotated[QueueProvider, Depends(get_queue_provider)]
