"""Worker task-endpoint tests (§18, §19, [D27]).

The HTTP status is how the worker talks to Cloud Tasks: 5xx means retry, 2xx means
done. That makes this mapping part of the retry policy rather than cosmetic, which is
why it is tested directly.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.integrations.queue import TaskRequest, TaskType
from app.worker.handlers.campaign_send import RetryableSendError
from app.worker.registry import task_registry


class TestRetrySignalling:
    def test_a_retryable_failure_asks_cloud_tasks_to_retry(self, worker_client: TestClient) -> None:
        """503, because Cloud Tasks retries on 5xx and treats any 2xx as final."""

        async def handler(task: TaskRequest) -> None:
            raise RetryableSendError("provider unavailable", retry_after_seconds=8.0)

        task_registry.register(TaskType.GENERATE_EMBEDDINGS, handler)

        response = worker_client.post(
            "/internal/tasks",
            json={"task_type": "generate_embeddings", "task_id": "t-1"},
        )
        assert response.status_code == 503

    def test_the_computed_backoff_is_returned(self, worker_client: TestClient) -> None:
        """[D27]: the jittered delay travels to the transport, so it waits the
        computed interval rather than its own default."""

        async def handler(task: TaskRequest) -> None:
            raise RetryableSendError("slow down", retry_after_seconds=12.7)

        task_registry.register(TaskType.GENERATE_EMBEDDINGS, handler)

        response = worker_client.post(
            "/internal/tasks",
            json={"task_type": "generate_embeddings", "task_id": "t-2"},
        )
        assert response.headers["Retry-After"] == "12"

    def test_a_sub_second_backoff_still_asks_for_at_least_one_second(
        self, worker_client: TestClient
    ) -> None:
        """Retry-After is expressed in whole seconds; zero would mean immediately."""

        async def handler(task: TaskRequest) -> None:
            raise RetryableSendError("brief", retry_after_seconds=0.3)

        task_registry.register(TaskType.GENERATE_EMBEDDINGS, handler)

        response = worker_client.post(
            "/internal/tasks",
            json={"task_type": "generate_embeddings", "task_id": "t-3"},
        )
        assert response.headers["Retry-After"] == "1"


class TestFinalOutcomes:
    def test_a_successful_task_is_final(self, worker_client: TestClient) -> None:
        async def handler(task: TaskRequest) -> None:
            return None

        task_registry.register(TaskType.GENERATE_EMBEDDINGS, handler)

        response = worker_client.post(
            "/internal/tasks",
            json={"task_type": "generate_embeddings", "task_id": "t-4"},
        )
        assert response.status_code == 200

    def test_a_handler_that_settles_a_permanent_failure_is_final(
        self, worker_client: TestClient
    ) -> None:
        """A permanent failure is recorded on the recipient and the task succeeds.
        Returning 5xx would make Cloud Tasks retry something that cannot succeed."""

        async def handler(task: TaskRequest) -> dict[str, str]:
            return {"result": "failed_permanent"}

        task_registry.register(TaskType.GENERATE_EMBEDDINGS, handler)

        response = worker_client.post(
            "/internal/tasks",
            json={"task_type": "generate_embeddings", "task_id": "t-5"},
        )
        assert response.status_code == 200

    def test_an_unknown_task_type_is_never_retried(self, worker_client: TestClient) -> None:
        """400, because redelivering something nothing handles would retry forever.

        `generate_embeddings` is the only declared task type with no handler — every
        other one is now registered.
        """
        response = worker_client.post(
            "/internal/tasks", json={"task_type": "generate_embeddings", "task_id": "t-6"}
        )
        assert response.status_code == 400

    def test_an_unexpected_error_is_retried(self, worker_client: TestClient) -> None:
        """500 rather than 200: an unrecognised failure may well be transient, and
        Cloud Tasks gives up eventually of its own accord."""

        async def handler(task: TaskRequest) -> None:
            raise RuntimeError("something unexpected")

        task_registry.register(TaskType.GENERATE_EMBEDDINGS, handler)

        with pytest.raises(RuntimeError):
            # TestClient re-raises rather than returning 500; in production the
            # exception handler turns this into a 500 that Cloud Tasks retries.
            worker_client.post(
                "/internal/tasks",
                json={"task_type": "generate_embeddings", "task_id": "t-7"},
            )


class TestTaskAuthentication:
    def test_no_credentials_are_required_when_unconfigured(self, worker_client: TestClient) -> None:
        """Local development and tests run without OIDC; the check activates only
        when a service account is configured."""

        async def handler(task: TaskRequest) -> None:
            return None

        task_registry.register(TaskType.GENERATE_EMBEDDINGS, handler)

        response = worker_client.post(
            "/internal/tasks",
            json={"task_type": "generate_embeddings", "task_id": "t-8"},
        )
        assert response.status_code == 200

    def test_a_missing_token_is_rejected_when_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """§18/§19: without this the task endpoint is open to anyone who can reach
        the Cloud Run URL."""
        from app.core.config import Settings, reset_settings_cache
        from app.worker.main import create_worker_app

        monkeypatch.setenv("WORKER_SERVICE_ACCOUNT_EMAIL", "worker@proj.iam.gserviceaccount.com")
        monkeypatch.setenv("WORKER_BASE_URL", "https://worker.example.com")
        reset_settings_cache()

        with TestClient(create_worker_app(Settings())) as client:
            response = client.post(
                "/internal/tasks",
                json={"task_type": "generate_embeddings", "task_id": "t-9"},
            )
            assert response.status_code == 401

        reset_settings_cache()

    def test_a_malformed_authorization_header_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.core.config import Settings, reset_settings_cache
        from app.worker.main import create_worker_app

        monkeypatch.setenv("WORKER_SERVICE_ACCOUNT_EMAIL", "worker@proj.iam.gserviceaccount.com")
        reset_settings_cache()

        with TestClient(create_worker_app(Settings())) as client:
            response = client.post(
                "/internal/tasks",
                json={"task_type": "generate_embeddings", "task_id": "t-10"},
                headers={"Authorization": "Basic abc123"},
            )
            assert response.status_code == 401

        reset_settings_cache()
