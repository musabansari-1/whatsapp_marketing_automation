"""Authentication endpoint tests against real PostgreSQL.

Covers the three cases the phase prompt names — successful login, failed login,
unauthorized access — plus the session lifecycle behaviour that makes [D2]'s
revocability meaningful.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.api.main import API_V1_PREFIX, create_app
from app.core.config import Settings, reset_settings_cache
from app.persistence.models import User
from tests.integration.conftest import ADMIN_EMAIL, SALES_EMAIL, login
from tests.integration.helpers import get_test_database_url, run_db

pytestmark = pytest.mark.integration


class TestSuccessfulLogin:
    def test_returns_the_user(self, client: TestClient, seeded_users: dict[str, str]) -> None:
        response = login(client)
        assert response.status_code == 200
        body = response.json()
        assert body["email"] == ADMIN_EMAIL
        assert body["role"] == "ADMIN"

    def test_never_returns_the_password_hash(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        assert "password_hash" not in login(client).json()
        assert "password" not in login(client).json()

    def test_sets_a_hardened_session_cookie(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        response = login(client)
        cookie_header = response.headers["set-cookie"]

        assert "wam_session=" in cookie_header
        assert "HttpOnly" in cookie_header
        # Lax, never None: [D13]'s same-origin BFF means None is unnecessary and [D2]
        # rejects it as the default architecture.
        assert "samesite=lax" in cookie_header.lower()
        assert "samesite=none" not in cookie_header.lower()

    def test_cookie_has_no_domain_attribute(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        """[D13]: the BFF forwards this Set-Cookie and the browser scopes it to the
        frontend origin. A Domain attribute would pin it to the API host instead."""
        assert "Domain=" not in login(client).headers["set-cookie"]

    def test_email_is_case_insensitive(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        assert login(client, email="ADMIN@EXAMPLE.COM").status_code == 200

    def test_each_login_issues_a_distinct_session(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        first = login(client).cookies["wam_session"]
        client.cookies.clear()
        second = login(client).cookies["wam_session"]
        assert first != second


class TestFailedLogin:
    def test_wrong_password_is_rejected(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        response = login(client, password="wrong-password")  # noqa: S106 - test fixture
        assert response.status_code == 401
        assert "set-cookie" not in response.headers

    def test_unknown_email_is_rejected(self, client: TestClient) -> None:
        assert login(client, email="nobody@example.com").status_code == 401

    def test_failures_are_indistinguishable(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        """Unknown address and wrong password must look identical, or the endpoint
        becomes a user-enumeration oracle."""
        unknown = login(client, email="nobody@example.com")
        wrong = login(client, password="wrong-password")  # noqa: S106 - test fixture
        assert unknown.status_code == wrong.status_code
        assert unknown.json() == wrong.json()

    def test_inactive_user_cannot_log_in(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        async def deactivate(session: AsyncSession) -> None:
            await session.execute(
                update(User).where(User.email == SALES_EMAIL).values(is_active=False)
            )

        run_db(deactivate)

        # Same 401 as a bad password: whether an account is disabled is not something
        # an unauthenticated caller should be able to learn.
        assert login(client, email=SALES_EMAIL).status_code == 401

    def test_malformed_body_is_rejected(self, client: TestClient) -> None:
        assert client.post(f"{API_V1_PREFIX}/auth/login", json={"email": "x"}).status_code == 422


class TestUnauthorizedAccess:
    def test_me_requires_authentication(self, client: TestClient) -> None:
        assert client.get(f"{API_V1_PREFIX}/auth/me").status_code == 401

    def test_users_list_requires_authentication(self, client: TestClient) -> None:
        assert client.get(f"{API_V1_PREFIX}/users").status_code == 401

    def test_garbage_cookie_is_rejected(self, client: TestClient) -> None:
        client.cookies.set("wam_session", "not-a-real-token")
        assert client.get(f"{API_V1_PREFIX}/auth/me").status_code == 401

    def test_no_registration_endpoint_exists(self, client: TestClient) -> None:
        """[D2]: there is no public registration path."""
        for path in ("/auth/register", "/auth/signup", "/register"):
            assert client.post(f"{API_V1_PREFIX}{path}", json={}).status_code == 404


class TestAuthenticatedAccess:
    def test_me_returns_the_logged_in_user(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        login(client)
        response = client.get(f"{API_V1_PREFIX}/auth/me")
        assert response.status_code == 200
        assert response.json()["email"] == ADMIN_EMAIL

    def test_session_persists_across_requests(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        login(client)
        assert client.get(f"{API_V1_PREFIX}/auth/me").status_code == 200
        assert client.get(f"{API_V1_PREFIX}/auth/me").status_code == 200


class TestLogout:
    def test_logout_ends_the_session(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        login(client)
        assert client.post(f"{API_V1_PREFIX}/auth/logout").status_code == 200
        assert client.get(f"{API_V1_PREFIX}/auth/me").status_code == 401

    def test_revoked_token_stops_working_even_if_replayed(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        """The point of server-side sessions: a captured cookie dies at logout rather
        than living until expiry."""
        token = login(client).cookies["wam_session"]
        client.post(f"{API_V1_PREFIX}/auth/logout")

        client.cookies.set("wam_session", token)
        assert client.get(f"{API_V1_PREFIX}/auth/me").status_code == 401

    def test_logout_without_a_session_still_succeeds(self, client: TestClient) -> None:
        assert client.post(f"{API_V1_PREFIX}/auth/logout").status_code == 200


class TestSecureCookie:
    def test_secure_flag_is_set_when_configured(
        self,
        engine: AsyncEngine,
        seeded_users: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Production sets Secure, so the cookie never traverses plaintext.

        Asserted on the response header rather than through a round trip, because a
        cookie jar will not replay a Secure cookie over the http TestClient uses —
        which is exactly the protection being verified.
        """
        from app.persistence import database

        monkeypatch.setenv("DATABASE_URL", get_test_database_url())
        monkeypatch.setenv("SESSION_COOKIE_SECURE", "true")
        reset_settings_cache()
        database._engine = None  # noqa: SLF001
        database._session_factory = None  # noqa: SLF001

        with TestClient(create_app(Settings())) as secure_client:
            response = login(secure_client)
            assert response.status_code == 200
            assert "secure" in response.headers["set-cookie"].lower()

        database._engine = None  # noqa: SLF001
        database._session_factory = None  # noqa: SLF001
        reset_settings_cache()
