"""API service tests."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.main import API_V1_PREFIX, create_app
from app.core.config import Settings
from app.core.correlation import CORRELATION_ID_HEADER


class TestHealth:
    def test_liveness_is_ok(self, api_client: TestClient) -> None:
        response = api_client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["service"] == "api"
        assert body["environment"] == "test"

    def test_liveness_also_under_v1(self, api_client: TestClient) -> None:
        assert api_client.get(f"{API_V1_PREFIX}/health").status_code == 200

    def test_liveness_does_not_touch_the_database(
        self, api_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Liveness must stay up when the database is down."""
        monkeypatch.setattr(
            "app.api.v1.routes.health.check_database_connection",
            _always_fail,
        )
        assert api_client.get("/health").status_code == 200


async def _always_fail() -> bool:
    return False


async def _always_ok() -> bool:
    return True


class TestReadiness:
    def test_ready_when_database_reachable(
        self, api_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("app.api.v1.routes.health.check_database_connection", _always_ok)
        response = api_client.get("/readiness")
        assert response.status_code == 200
        assert response.json() == {"status": "ready", "checks": {"database": True}}

    def test_not_ready_when_database_unreachable(
        self, api_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("app.api.v1.routes.health.check_database_connection", _always_fail)
        response = api_client.get("/readiness")
        assert response.status_code == 503
        assert response.json()["status"] == "not_ready"


class TestCorrelationMiddleware:
    def test_generates_and_echoes_an_id(self, api_client: TestClient) -> None:
        response = api_client.get("/health")
        assert response.headers[CORRELATION_ID_HEADER]

    def test_forwards_the_incoming_id(self, api_client: TestClient) -> None:
        """The BFF forwards its ID ([D13]); the API must adopt rather than replace it."""
        response = api_client.get("/health", headers={CORRELATION_ID_HEADER: "from-bff"})
        assert response.headers[CORRELATION_ID_HEADER] == "from-bff"

    def test_ids_differ_between_requests(self, api_client: TestClient) -> None:
        first = api_client.get("/health").headers[CORRELATION_ID_HEADER]
        second = api_client.get("/health").headers[CORRELATION_ID_HEADER]
        assert first != second


class TestAppConfiguration:
    def test_docs_available_outside_production(self, api_client: TestClient) -> None:
        assert api_client.get("/openapi.json").status_code == 200

    def test_docs_disabled_in_production(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """§19: protect internal APIs."""
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("APP_SECRET", "a-real-secret")
        with TestClient(create_app(Settings())) as client:
            assert client.get("/openapi.json").status_code == 404
            assert client.get("/docs").status_code == 404

    def test_no_cors_headers_by_default(self, api_client: TestClient) -> None:
        """[D13]: same-origin BFF means no cross-origin browser requests to allow."""
        response = api_client.get("/health", headers={"Origin": "https://evil.example.com"})
        assert "access-control-allow-origin" not in {k.lower() for k in response.headers}

    def test_unknown_route_is_404(self, api_client: TestClient) -> None:
        assert api_client.get(f"{API_V1_PREFIX}/leads").status_code == 404
