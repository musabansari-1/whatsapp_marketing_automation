"""Google Cloud Tasks queue provider (specification §18, [D27], [D28]).

The production transport behind `QueueProvider`. Nothing above `app.integrations`
imports this module; it is selected by configuration.

Two Cloud Tasks features do real work here rather than being reimplemented:

**Task naming gives deduplication for free.** A task created with an explicit name
cannot be created twice — Cloud Tasks rejects the duplicate with ALREADY_EXISTS. Since
Phase 5 already derives deterministic task IDs from the recipient, naming tasks after
them means a re-triggered broadcast cannot enqueue the same send twice, before any of
the worker's own idempotency is reached.

**Retries and backoff are the queue's job.** The queue is configured with the [D27]
policy; the worker signals retry by returning 503 with Retry-After. Reimplementing
backoff in the application would fight the queue rather than help it.

Client-side pacing ([D28]) spreads a large campaign over time by scheduling tasks into
the future, so a 50,000-recipient broadcast does not arrive at Meta as an instant
burst. The rate is configuration with no default, because assuming a messaging tier is
exactly what [D28] forbids.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.errors import NonRetryableProviderError, RetryableProviderError
from app.core.logging import get_logger
from app.integrations.queue.provider import EnqueuedTask, QueueProvider, TaskRequest

logger = get_logger(__name__)

# Cloud Tasks names must be URL-safe and unique within the queue.
_SAFE_NAME = str.maketrans({":": "-", "/": "-", ".": "-", "_": "-"})


class CloudTasksProvider(QueueProvider):
    """Creates HTTP tasks that Cloud Run executes."""

    name = "cloud_tasks"

    def __init__(
        self,
        *,
        project: str,
        location: str,
        queue: str,
        worker_base_url: str,
        service_account_email: str | None = None,
        dispatch_rate_per_second: float | None = None,
        client: Any | None = None,
    ) -> None:
        self._project = project
        self._location = location
        self._queue = queue
        self._worker_url = f"{worker_base_url.rstrip('/')}/internal/tasks"
        self._service_account_email = service_account_email
        self._dispatch_rate = dispatch_rate_per_second
        self._client = client
        # Tracks where in the pacing schedule the next task should land.
        self._next_slot: datetime | None = None

    @property
    def queue_path(self) -> str:
        client = self._get_client()
        path: str = client.queue_path(self._project, self._location, self._queue)
        return path

    async def enqueue(self, task: TaskRequest) -> EnqueuedTask:
        """Create one Cloud Task.

        A duplicate name is treated as success, not failure: it means the task already
        exists, which is precisely the outcome the caller wanted (§20).
        """
        from google.api_core import exceptions as gcp_exceptions

        client = self._get_client()
        request: dict[str, Any] = {
            "parent": self.queue_path,
            "task": self._build_task(task),
        }

        try:
            created = client.create_task(request=request)
        except gcp_exceptions.AlreadyExists:
            # The deterministic name did its job. Nothing to do and nothing wrong.
            logger.info("cloud_task_duplicate_ignored", task_id=task.task_id)
            return EnqueuedTask(task_id=task.task_id, task_type=task.task_type)
        except (gcp_exceptions.ServiceUnavailable, gcp_exceptions.DeadlineExceeded) as exc:
            raise RetryableProviderError(
                f"Cloud Tasks unavailable: {exc}", provider=self.name
            ) from exc
        except gcp_exceptions.PermissionDenied as exc:
            # Retrying with the same rejected credentials cannot help.
            raise NonRetryableProviderError(
                f"Cloud Tasks permission denied: {exc}", provider=self.name
            ) from exc
        except gcp_exceptions.GoogleAPICallError as exc:
            raise RetryableProviderError(f"Cloud Tasks error: {exc}", provider=self.name) from exc

        logger.info("cloud_task_created", task_id=task.task_id, task_type=task.task_type.value)
        return EnqueuedTask(
            task_id=task.task_id,
            task_type=task.task_type,
            provider_task_name=getattr(created, "name", None),
        )

    # ---------------------------------------------------------------- internals

    def _build_task(self, task: TaskRequest) -> dict[str, Any]:
        body = json.dumps(
            {
                "task_type": task.task_type.value,
                "task_id": task.task_id,
                "payload": task.payload,
                "correlation_id": task.correlation_id,
            }
        ).encode()

        http_request: dict[str, Any] = {
            "http_method": "POST",
            "url": self._worker_url,
            "headers": {"Content-Type": "application/json"},
            "body": body,
        }

        if self._service_account_email:
            # §18: every task authenticated. Cloud Tasks mints a Google-signed OIDC
            # token the worker verifies.
            http_request["oidc_token"] = {
                "service_account_email": self._service_account_email,
                "audience": self._worker_url.rsplit("/internal/tasks", 1)[0],
            }

        built: dict[str, Any] = {
            # Naming the task is what makes creation idempotent.
            "name": f"{self.queue_path}/tasks/{self._safe_name(task.task_id)}",
            "http_request": http_request,
        }

        schedule_time = self._schedule_time(task)
        if schedule_time is not None:
            built["schedule_time"] = schedule_time

        return built

    def _schedule_time(self, task: TaskRequest) -> datetime | None:
        """When this task should run.

        An explicit `scheduled_for` wins. Otherwise, when a dispatch rate is
        configured, tasks are spread across successive slots so a large campaign
        trickles rather than floods ([D28]).
        """
        if task.scheduled_for is not None:
            return task.scheduled_for

        if not self._dispatch_rate:
            return None

        interval = timedelta(seconds=1.0 / self._dispatch_rate)
        now = datetime.now(UTC)

        # Restart the schedule if the previous burst has already drained, so a later
        # campaign is not queued behind an earlier one's stale slots.
        if self._next_slot is None or self._next_slot < now:
            self._next_slot = now

        slot = self._next_slot
        self._next_slot = slot + interval
        return slot

    @staticmethod
    def _safe_name(task_id: str) -> str:
        return task_id.translate(_SAFE_NAME)[:500]

    def _get_client(self) -> Any:
        if self._client is None:
            from google.cloud import tasks_v2

            self._client = tasks_v2.CloudTasksClient()
        return self._client
