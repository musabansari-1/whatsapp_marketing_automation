"""User management endpoint tests ([D2], [D14]).

The central question is authorization: every route here must be closed to a
salesperson and to an anonymous caller.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.api.main import API_V1_PREFIX
from tests.integration.conftest import ADMIN_EMAIL, PASSWORD, SALES_EMAIL, login

pytestmark = pytest.mark.integration

NEW_USER = {
    "email": "newperson@example.com",
    "password": "another-long-enough-password",
    "full_name": "New Person",
    "role": "SALESPERSON",
}


class TestAdminCanManageUsers:
    def test_create_user(self, client: TestClient, seeded_users: dict[str, str]) -> None:
        login(client)
        response = client.post(f"{API_V1_PREFIX}/users", json=NEW_USER)

        assert response.status_code == 201
        body = response.json()
        assert body["email"] == NEW_USER["email"]
        assert body["role"] == "SALESPERSON"
        assert body["is_active"] is True
        assert "password" not in body
        assert "password_hash" not in body

    def test_created_user_can_log_in(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        login(client)
        client.post(f"{API_V1_PREFIX}/users", json=NEW_USER)
        client.post(f"{API_V1_PREFIX}/auth/logout")

        assert login(client, NEW_USER["email"], NEW_USER["password"]).status_code == 200

    def test_duplicate_email_is_rejected(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        login(client)
        assert (
            client.post(
                f"{API_V1_PREFIX}/users", json={**NEW_USER, "email": ADMIN_EMAIL}
            ).status_code
            == 409
        )

    def test_short_password_is_rejected(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        login(client)
        assert (
            client.post(
                f"{API_V1_PREFIX}/users", json={**NEW_USER, "password": "short"}
            ).status_code
            == 422
        )

    def test_invalid_role_is_rejected(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        login(client)
        assert (
            client.post(
                f"{API_V1_PREFIX}/users", json={**NEW_USER, "role": "SUPERUSER"}
            ).status_code
            == 422
        )

    def test_list_users(self, client: TestClient, seeded_users: dict[str, str]) -> None:
        login(client)
        response = client.get(f"{API_V1_PREFIX}/users")

        assert response.status_code == 200
        emails = {u["email"] for u in response.json()}
        assert {ADMIN_EMAIL, SALES_EMAIL} <= emails
        assert all("password_hash" not in u for u in response.json())

    def test_get_user_by_id(self, client: TestClient, seeded_users: dict[str, str]) -> None:
        login(client)
        created = client.post(f"{API_V1_PREFIX}/users", json=NEW_USER).json()

        response = client.get(f"{API_V1_PREFIX}/users/{created['id']}")
        assert response.status_code == 200
        assert response.json()["email"] == NEW_USER["email"]

    def test_unknown_user_is_404(self, client: TestClient, seeded_users: dict[str, str]) -> None:
        login(client)
        assert client.get(f"{API_V1_PREFIX}/users/{uuid.uuid4()}").status_code == 404

    def test_update_user_fields(self, client: TestClient, seeded_users: dict[str, str]) -> None:
        login(client)
        created = client.post(f"{API_V1_PREFIX}/users", json=NEW_USER).json()

        response = client.patch(
            f"{API_V1_PREFIX}/users/{created['id']}",
            json={"full_name": "Renamed", "role": "ADMIN"},
        )
        assert response.status_code == 200
        assert response.json()["full_name"] == "Renamed"
        assert response.json()["role"] == "ADMIN"

    def test_omitted_fields_are_unchanged(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        login(client)
        created = client.post(f"{API_V1_PREFIX}/users", json=NEW_USER).json()

        response = client.patch(f"{API_V1_PREFIX}/users/{created['id']}", json={"full_name": "X"})
        assert response.json()["role"] == "SALESPERSON"
        assert response.json()["is_active"] is True


class TestDeactivation:
    def test_deactivated_user_cannot_log_in(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        login(client)
        created = client.post(f"{API_V1_PREFIX}/users", json=NEW_USER).json()
        client.patch(f"{API_V1_PREFIX}/users/{created['id']}", json={"is_active": False})
        client.post(f"{API_V1_PREFIX}/auth/logout")

        assert login(client, NEW_USER["email"], NEW_USER["password"]).status_code == 401

    def test_deactivation_revokes_live_sessions(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        """[D2]'s revocability would be hollow if a deactivated user kept working
        until their session happened to expire."""
        login(client)
        created = client.post(f"{API_V1_PREFIX}/users", json=NEW_USER).json()
        client.post(f"{API_V1_PREFIX}/auth/logout")

        # The new user establishes a session.
        login(client, NEW_USER["email"], NEW_USER["password"])
        victim_token = client.cookies["wam_session"]
        assert client.get(f"{API_V1_PREFIX}/auth/me").status_code == 200

        # An admin deactivates them.
        client.cookies.clear()
        login(client)
        client.patch(f"{API_V1_PREFIX}/users/{created['id']}", json={"is_active": False})

        # The previously valid session is dead immediately.
        client.cookies.clear()
        client.cookies.set("wam_session", victim_token)
        assert client.get(f"{API_V1_PREFIX}/auth/me").status_code == 401

    def test_admin_cannot_deactivate_themselves(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        """Prevents an administrator locking everyone out of the system."""
        login(client)
        me = client.get(f"{API_V1_PREFIX}/auth/me").json()

        response = client.patch(f"{API_V1_PREFIX}/users/{me['id']}", json={"is_active": False})
        assert response.status_code == 400
        assert client.get(f"{API_V1_PREFIX}/auth/me").status_code == 200

    def test_password_change_revokes_sessions(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        """A credential change must not leave an old session usable."""
        login(client)
        created = client.post(f"{API_V1_PREFIX}/users", json=NEW_USER).json()
        client.post(f"{API_V1_PREFIX}/auth/logout")

        login(client, NEW_USER["email"], NEW_USER["password"])
        old_token = client.cookies["wam_session"]

        client.cookies.clear()
        login(client)
        client.patch(
            f"{API_V1_PREFIX}/users/{created['id']}",
            json={"password": "a-brand-new-long-password"},
        )

        client.cookies.clear()
        client.cookies.set("wam_session", old_token)
        assert client.get(f"{API_V1_PREFIX}/auth/me").status_code == 401


class TestSalespersonIsForbidden:
    """[D14]: user management is ADMIN-only."""

    def _login_as_sales(self, client: TestClient) -> None:
        assert login(client, SALES_EMAIL, PASSWORD).status_code == 200

    def test_cannot_create(self, client: TestClient, seeded_users: dict[str, str]) -> None:
        self._login_as_sales(client)
        assert client.post(f"{API_V1_PREFIX}/users", json=NEW_USER).status_code == 403

    def test_cannot_list(self, client: TestClient, seeded_users: dict[str, str]) -> None:
        self._login_as_sales(client)
        assert client.get(f"{API_V1_PREFIX}/users").status_code == 403

    def test_cannot_read_one(self, client: TestClient, seeded_users: dict[str, str]) -> None:
        self._login_as_sales(client)
        assert client.get(f"{API_V1_PREFIX}/users/{uuid.uuid4()}").status_code == 403

    def test_cannot_update(self, client: TestClient, seeded_users: dict[str, str]) -> None:
        self._login_as_sales(client)
        response = client.patch(f"{API_V1_PREFIX}/users/{uuid.uuid4()}", json={"role": "ADMIN"})
        assert response.status_code == 403

    def test_forbidden_not_unauthorized(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        """403, not 401: the caller is authenticated, just not permitted. Conflating
        them would bounce a logged-in salesperson to the login page."""
        self._login_as_sales(client)
        assert client.get(f"{API_V1_PREFIX}/users").status_code == 403
        assert client.get(f"{API_V1_PREFIX}/auth/me").status_code == 200


class TestAnonymousIsRejected:
    def test_every_route_requires_authentication(self, client: TestClient) -> None:
        assert client.post(f"{API_V1_PREFIX}/users", json=NEW_USER).status_code == 401
        assert client.get(f"{API_V1_PREFIX}/users").status_code == 401
        assert client.get(f"{API_V1_PREFIX}/users/{uuid.uuid4()}").status_code == 401
        assert client.patch(f"{API_V1_PREFIX}/users/{uuid.uuid4()}", json={}).status_code == 401
