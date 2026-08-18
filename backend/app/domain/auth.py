"""Authentication and authorization policy.

Pure rules: no database, no HTTP, no framework. Everything here is decidable from
values the caller passes in, which keeps the security-relevant decisions testable in
isolation from transport and storage.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.domain.enums import UserRole


class AuthenticationError(Exception):
    """Credentials or session are not valid. Deliberately unspecific.

    Callers must not distinguish "no such user" from "wrong password" in anything
    the client can observe, or the API becomes a user-enumeration oracle.
    """


class AuthorizationError(Exception):
    """Authenticated, but not permitted to perform this action."""


class InactiveUserError(AuthenticationError):
    """The account exists but has been deactivated."""


@dataclass(frozen=True, slots=True)
class SessionState:
    """The stored facts about a session, independent of how they were loaded."""

    expires_at: datetime
    revoked_at: datetime | None
    user_is_active: bool


def session_expiry(ttl_seconds: int, *, now: datetime | None = None) -> datetime:
    """When a session created now should expire."""
    if ttl_seconds <= 0:
        raise ValueError("session ttl must be positive")
    return (now or datetime.now(UTC)) + timedelta(seconds=ttl_seconds)


def is_session_valid(state: SessionState, *, now: datetime | None = None) -> bool:
    """A session is usable only if unrevoked, unexpired and owned by an active user.

    The active check lives here so deactivating a user immediately ends their access
    rather than waiting for the session to expire.
    """
    moment = now or datetime.now(UTC)
    if state.revoked_at is not None:
        return False
    if state.expires_at <= moment:
        return False
    return state.user_is_active


def can_manage_users(role: UserRole) -> bool:
    """[D2]/[D14]: only ADMIN creates and manages internal users."""
    return role is UserRole.ADMIN


def require_role(actual: UserRole, required: UserRole) -> None:
    """Raise unless the actor holds the required role.

    ADMIN is a superset of SALESPERSON, so an admin satisfies any requirement.
    """
    if actual is required or actual is UserRole.ADMIN:
        return
    raise AuthorizationError(f"requires {required.value}")


def can_deactivate(actor_id: object, target_id: object) -> bool:
    """An admin must not deactivate their own account.

    Guards against the last administrator locking everyone out, which would leave the
    system recoverable only by re-running the bootstrap command.
    """
    return actor_id != target_id
