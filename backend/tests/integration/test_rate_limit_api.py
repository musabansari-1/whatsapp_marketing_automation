"""Rate limit enforcement tests (§19, D-42, D-45).

The counter is shared through the database, so these need a real one — an in-process
counter would pass a test and fail in production, which is the whole reason the table
exists.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.rate_limit import RateLimitRule, RateLimitScope
from app.persistence.models import RateLimitCounter
from app.persistence.repositories import rate_limits as rate_limit_repo
from tests.integration.conftest import ADMIN_EMAIL, PASSWORD, SALES_EMAIL
from tests.integration.helpers import run_db

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def clean_counters(engine: Any) -> Iterator[None]:
    async def clear(session: AsyncSession) -> None:
        await session.execute(delete(RateLimitCounter))

    run_db(clear)
    yield
    run_db(clear)


def attempt_login(client: TestClient, *, email: str, password: str, ip: str = "203.0.113.7") -> Any:
    return client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
        # Behind Cloud Run the socket is the load balancer, so this is what identifies
        # the caller.
        headers={"X-Forwarded-For": ip},
    )


class TestLoginRateLimit:
    def test_a_correct_login_still_works(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        """The limit must not be so eager that it breaks ordinary use."""
        assert attempt_login(client, email=ADMIN_EMAIL, password=PASSWORD).status_code == 200

    def test_repeated_failures_are_eventually_refused(
        self, client: TestClient, seeded_users: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """§19. Argon2id makes each guess expensive, which is mitigation rather than a
        limit — and the CPU cost is ours."""
        from app.core.config import get_settings

        settings = get_settings()
        monkeypatch.setattr(settings, "rate_limit_login_account_attempts", 3)

        codes = [
            attempt_login(client, email=ADMIN_EMAIL, password="wrong-password").status_code
            for _ in range(5)
        ]

        assert codes[:3] == [401, 401, 401]
        assert 429 in codes

    def test_a_refusal_says_when_to_come_back(
        self, client: TestClient, seeded_users: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.core.config import get_settings

        monkeypatch.setattr(get_settings(), "rate_limit_login_account_attempts", 1)

        attempt_login(client, email=ADMIN_EMAIL, password="wrong-password")
        refused = attempt_login(client, email=ADMIN_EMAIL, password="wrong-password")

        assert refused.status_code == 429
        assert int(refused.headers["Retry-After"]) >= 1

    def test_the_limit_applies_to_successful_attempts_too(
        self, client: TestClient, seeded_users: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Counting only failures would let an attacker who guesses correctly keep going,
        and would make the endpoint a free password oracle."""
        from app.core.config import get_settings

        monkeypatch.setattr(get_settings(), "rate_limit_login_account_attempts", 2)

        attempt_login(client, email=ADMIN_EMAIL, password=PASSWORD)
        attempt_login(client, email=ADMIN_EMAIL, password=PASSWORD)
        third = attempt_login(client, email=ADMIN_EMAIL, password=PASSWORD)

        assert third.status_code == 429

    def test_one_account_cannot_exhaust_another(
        self, client: TestClient, seeded_users: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Otherwise an attacker could lock out every user by attacking one."""
        from app.core.config import get_settings

        settings = get_settings()
        monkeypatch.setattr(settings, "rate_limit_login_account_attempts", 2)
        # Raised so the per-IP limit does not fire first and mask the point.
        monkeypatch.setattr(settings, "rate_limit_login_ip_attempts", 100)

        for _ in range(3):
            attempt_login(client, email=ADMIN_EMAIL, password="wrong-password")

        other = attempt_login(client, email=SALES_EMAIL, password=PASSWORD)
        assert other.status_code == 200

    def test_the_account_limit_ignores_capitalisation(
        self, client: TestClient, seeded_users: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Varying the spelling must not buy a fresh allowance."""
        from app.core.config import get_settings

        settings = get_settings()
        monkeypatch.setattr(settings, "rate_limit_login_account_attempts", 2)
        monkeypatch.setattr(settings, "rate_limit_login_ip_attempts", 100)

        attempt_login(client, email=ADMIN_EMAIL, password="wrong-password")
        attempt_login(client, email=ADMIN_EMAIL.upper(), password="wrong-password")
        third = attempt_login(client, email=ADMIN_EMAIL.title(), password="wrong-password")

        assert third.status_code == 429

    def test_a_different_address_has_its_own_ip_budget(
        self, client: TestClient, seeded_users: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.core.config import get_settings

        settings = get_settings()
        monkeypatch.setattr(settings, "rate_limit_login_ip_attempts", 2)
        monkeypatch.setattr(settings, "rate_limit_login_account_attempts", 100)

        for _ in range(3):
            attempt_login(client, email=ADMIN_EMAIL, password="wrong", ip="198.51.100.1")

        elsewhere = attempt_login(client, email=ADMIN_EMAIL, password=PASSWORD, ip="198.51.100.2")
        assert elsewhere.status_code == 200

    def test_an_unknown_email_is_still_counted(
        self, client: TestClient, seeded_users: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Otherwise enumeration is unlimited as long as the guesses are wrong."""
        from app.core.config import get_settings

        monkeypatch.setattr(get_settings(), "rate_limit_login_ip_attempts", 2)

        codes = [
            attempt_login(client, email=f"nobody{n}@example.com", password="x").status_code
            for n in range(4)
        ]

        assert 429 in codes


class TestUploadRateLimit:
    def test_uploads_are_limited_per_user(
        self, client: TestClient, seeded_users: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Each upload costs parsing and embedding work in the worker."""
        import io

        from app.core.config import get_settings
        from tests.integration.conftest import login

        login(client)
        monkeypatch.setattr(get_settings(), "rate_limit_upload_requests", 2)

        base = client.post(
            "/api/v1/knowledge-bases", json={"name": "Limits", "description": None}
        ).json()["id"]

        codes = []
        for index in range(4):
            response = client.post(
                f"/api/v1/knowledge-bases/{base}/documents",
                files={
                    "file": (
                        f"doc{index}.txt",
                        io.BytesIO(f"Document number {index}.".encode()),
                        "text/plain",
                    )
                },
            )
            codes.append(response.status_code)

        assert 429 in codes


class TestCounterStorage:
    def test_the_counter_is_shared_not_per_process(self) -> None:
        """The reason this is a table at all: Cloud Run runs several instances, and an
        in-process counter would divide the effective limit by the instance count."""
        rule = RateLimitRule(limit=10, window_seconds=900)

        async def count_twice(session: AsyncSession) -> tuple[int, int]:
            first = await rate_limit_repo.hit(
                session, scope=RateLimitScope.LOGIN_IP, identifier="shared", rule=rule
            )
            second = await rate_limit_repo.hit(
                session, scope=RateLimitScope.LOGIN_IP, identifier="shared", rule=rule
            )
            return first.current, second.current

        first, second = run_db(count_twice)
        assert (first, second) == (1, 2)

    def test_expired_counters_are_removed(self) -> None:
        """Housekeeping: an expired window is already unreachable, but the rows would
        otherwise accumulate forever."""
        from datetime import UTC, datetime, timedelta

        rule = RateLimitRule(limit=5, window_seconds=60)

        async def make_stale(session: AsyncSession) -> None:
            await rate_limit_repo.hit(
                session,
                scope=RateLimitScope.LOGIN_IP,
                identifier="stale",
                rule=rule,
                now=datetime.now(UTC) - timedelta(hours=2),
            )

        run_db(make_stale)

        async def purge(session: AsyncSession) -> int:
            return await rate_limit_repo.delete_expired(session)

        assert run_db(purge) >= 1

    def test_a_live_counter_survives_the_purge(self) -> None:
        rule = RateLimitRule(limit=5, window_seconds=3600)

        async def make_live(session: AsyncSession) -> None:
            await rate_limit_repo.hit(
                session, scope=RateLimitScope.LOGIN_IP, identifier="live", rule=rule
            )

        run_db(make_live)
        run_db(lambda session: rate_limit_repo.delete_expired(session))

        async def read(session: AsyncSession) -> int:
            return await rate_limit_repo.current_count(
                session, scope=RateLimitScope.LOGIN_IP, identifier="live", rule=rule
            )

        assert run_db(read) == 1
