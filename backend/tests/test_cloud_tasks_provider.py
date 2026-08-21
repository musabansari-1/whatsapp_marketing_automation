"""Cloud Tasks provider tests (§18, §20, [D28]).

The Google client is mocked throughout; no GCP call is made and no credentials are
needed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from google.api_core import exceptions as gcp_exceptions

from app.core.errors import NonRetryableProviderError, RetryableProviderError
from app.integrations.queue import TaskRequest, TaskType
from app.integrations.queue.cloud_tasks import CloudTasksProvider


class FakeClient:
    """Stands in for `tasks_v2.CloudTasksClient`."""

    def __init__(self, error: Exception | None = None) -> None:
        self.created: list[dict[str, Any]] = []
        self.error = error

    def queue_path(self, project: str, location: str, queue: str) -> str:
        return f"projects/{project}/locations/{location}/queues/{queue}"

    def create_task(self, request: dict[str, Any]) -> Any:
        if self.error is not None:
            raise self.error
        self.created.append(request)
        return type("Task", (), {"name": request["task"]["name"]})()


def make_provider(client: FakeClient, **kwargs: Any) -> CloudTasksProvider:
    return CloudTasksProvider(
        project="proj",
        location="us-central1",
        queue="sends",
        worker_base_url="https://worker.example.com",
        client=client,
        **kwargs,
    )


def a_task(task_id: str = "send-abc") -> TaskRequest:
    return TaskRequest(
        task_type=TaskType.SEND_CAMPAIGN_MESSAGE,
        task_id=task_id,
        payload={"recipient_id": "r-1"},
    )


class TestTaskCreation:
    async def test_a_task_is_created_on_the_queue(self) -> None:
        client = FakeClient()
        await make_provider(client).enqueue(a_task())

        assert len(client.created) == 1
        assert client.created[0]["parent"] == ("projects/proj/locations/us-central1/queues/sends")

    async def test_the_task_targets_the_worker_endpoint(self) -> None:
        client = FakeClient()
        await make_provider(client).enqueue(a_task())

        http = client.created[0]["task"]["http_request"]
        assert http["url"] == "https://worker.example.com/internal/tasks"
        assert http["http_method"] == "POST"

    async def test_the_body_carries_the_task_envelope(self) -> None:
        client = FakeClient()
        await make_provider(client).enqueue(a_task())

        body = json.loads(client.created[0]["task"]["http_request"]["body"])
        assert body["task_type"] == "send_campaign_message"
        assert body["task_id"] == "send-abc"
        assert body["payload"] == {"recipient_id": "r-1"}

    async def test_the_task_is_named_after_its_id(self) -> None:
        """Naming is what makes creation idempotent: Cloud Tasks refuses a duplicate
        name, so a re-triggered broadcast cannot enqueue the same send twice (§20)."""
        client = FakeClient()
        await make_provider(client).enqueue(a_task("send-r-1"))

        assert client.created[0]["task"]["name"].endswith("/tasks/send-r-1")

    async def test_unsafe_characters_are_sanitised(self) -> None:
        """Cloud Tasks names must be URL-safe; a colon or slash would be rejected."""
        client = FakeClient()
        await make_provider(client).enqueue(a_task("webhook-abc:def/ghi"))

        name = client.created[0]["task"]["name"]
        assert ":" not in name.rsplit("/tasks/", 1)[1]


class TestAuthentication:
    async def test_an_oidc_token_is_attached_when_configured(self) -> None:
        """§18: every task authenticated."""
        client = FakeClient()
        await make_provider(
            client, service_account_email="worker@proj.iam.gserviceaccount.com"
        ).enqueue(a_task())

        token = client.created[0]["task"]["http_request"]["oidc_token"]
        assert token["service_account_email"] == "worker@proj.iam.gserviceaccount.com"
        assert token["audience"] == "https://worker.example.com"

    async def test_no_token_when_unconfigured(self) -> None:
        client = FakeClient()
        await make_provider(client).enqueue(a_task())
        assert "oidc_token" not in client.created[0]["task"]["http_request"]


class TestDuplicateHandling:
    async def test_a_duplicate_task_is_treated_as_success(self) -> None:
        """The deterministic name did its job. The caller wanted the task to exist,
        and it does — reporting failure would trigger a pointless retry (§20)."""
        client = FakeClient(error=gcp_exceptions.AlreadyExists("exists"))
        result = await make_provider(client).enqueue(a_task())

        assert result.task_id == "send-abc"
        assert client.created == []


class TestFailureClassification:
    @pytest.mark.parametrize(
        "error",
        [
            gcp_exceptions.ServiceUnavailable("down"),
            gcp_exceptions.DeadlineExceeded("slow"),
        ],
    )
    async def test_transient_gcp_errors_are_retryable(self, error: Exception) -> None:
        client = FakeClient(error=error)
        with pytest.raises(RetryableProviderError):
            await make_provider(client).enqueue(a_task())

    async def test_permission_denied_is_not_retryable(self) -> None:
        """Retrying with the same rejected credentials cannot help."""
        client = FakeClient(error=gcp_exceptions.PermissionDenied("nope"))
        with pytest.raises(NonRetryableProviderError):
            await make_provider(client).enqueue(a_task())


class TestDispatchPacing:
    """[D28]: configurable throttling, no assumed tier."""

    async def test_no_pacing_by_default(self) -> None:
        """Unset means no client-side pacing, which is correct for development and
        for a queue configured to pace itself."""
        client = FakeClient()
        await make_provider(client).enqueue(a_task())
        assert "schedule_time" not in client.created[0]["task"]

    async def test_tasks_are_spread_when_a_rate_is_configured(self) -> None:
        """A 50,000-recipient broadcast must trickle rather than flood."""
        client = FakeClient()
        provider = make_provider(client, dispatch_rate_per_second=10.0)

        for index in range(5):
            await provider.enqueue(a_task(f"send-{index}"))

        times = [task["task"]["schedule_time"] for task in client.created]
        gaps = [(times[i + 1] - times[i]).total_seconds() for i in range(len(times) - 1)]
        # 10 per second means 100ms apart.
        assert all(abs(gap - 0.1) < 0.001 for gap in gaps)

    async def test_a_higher_rate_packs_tasks_closer(self) -> None:
        client = FakeClient()
        provider = make_provider(client, dispatch_rate_per_second=100.0)

        await provider.enqueue(a_task("a"))
        await provider.enqueue(a_task("b"))

        gap = (
            client.created[1]["task"]["schedule_time"] - client.created[0]["task"]["schedule_time"]
        ).total_seconds()
        assert abs(gap - 0.01) < 0.001

    async def test_an_explicit_schedule_wins(self) -> None:
        client = FakeClient()
        when = datetime.now(UTC) + timedelta(hours=1)
        await make_provider(client, dispatch_rate_per_second=10.0).enqueue(
            TaskRequest(
                task_type=TaskType.SEND_CAMPAIGN_MESSAGE,
                task_id="later",
                scheduled_for=when,
            )
        )
        assert client.created[0]["task"]["schedule_time"] == when
