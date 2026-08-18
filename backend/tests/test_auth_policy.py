"""Authentication and authorization policy tests.

Pure rules, tested without a database or HTTP layer.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.domain.auth import (
    AuthorizationError,
    SessionState,
    can_deactivate,
    can_manage_users,
    is_session_valid,
    require_role,
    session_expiry,
)
from app.domain.enums import UserRole

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _state(
    *,
    expires_in: timedelta = timedelta(hours=1),
    revoked: bool = False,
    active: bool = True,
) -> SessionState:
    return SessionState(
        expires_at=NOW + expires_in,
        revoked_at=NOW - timedelta(minutes=1) if revoked else None,
        user_is_active=active,
    )


class TestSessionExpiry:
    def test_expiry_is_ttl_after_now(self) -> None:
        assert session_expiry(3600, now=NOW) == NOW + timedelta(hours=1)

    def test_non_positive_ttl_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            session_expiry(0, now=NOW)

    def test_expiry_is_timezone_aware(self) -> None:
        assert session_expiry(3600).tzinfo is not None


class TestSessionValidity:
    def test_fresh_session_is_valid(self) -> None:
        assert is_session_valid(_state(), now=NOW) is True

    def test_expired_session_is_invalid(self) -> None:
        assert is_session_valid(_state(expires_in=-timedelta(seconds=1)), now=NOW) is False

    def test_session_expiring_exactly_now_is_invalid(self) -> None:
        assert is_session_valid(_state(expires_in=timedelta(0)), now=NOW) is False

    def test_revoked_session_is_invalid(self) -> None:
        """This is what makes logout real rather than cosmetic."""
        assert is_session_valid(_state(revoked=True), now=NOW) is False

    def test_deactivated_user_invalidates_a_live_session(self) -> None:
        """Deactivation must take effect immediately, not when the session expires."""
        assert is_session_valid(_state(active=False), now=NOW) is False

    def test_revocation_wins_over_everything(self) -> None:
        assert is_session_valid(_state(revoked=True, active=True), now=NOW) is False


class TestAuthorization:
    def test_only_admin_manages_users(self) -> None:
        """[D2]/[D14]."""
        assert can_manage_users(UserRole.ADMIN) is True
        assert can_manage_users(UserRole.SALESPERSON) is False

    def test_salesperson_satisfies_salesperson_requirement(self) -> None:
        require_role(UserRole.SALESPERSON, UserRole.SALESPERSON)

    def test_admin_satisfies_salesperson_requirement(self) -> None:
        """ADMIN is a superset, so it passes any role requirement."""
        require_role(UserRole.ADMIN, UserRole.SALESPERSON)

    def test_salesperson_cannot_satisfy_admin_requirement(self) -> None:
        with pytest.raises(AuthorizationError):
            require_role(UserRole.SALESPERSON, UserRole.ADMIN)


class TestSelfDeactivation:
    def test_admin_cannot_deactivate_themselves(self) -> None:
        """Guards against locking every administrator out of the system."""
        admin_id = uuid.uuid4()
        assert can_deactivate(admin_id, admin_id) is False

    def test_admin_can_deactivate_someone_else(self) -> None:
        assert can_deactivate(uuid.uuid4(), uuid.uuid4()) is True
