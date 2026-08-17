"""In-memory queue for local development and automated tests.

Two modes, covering both words in the specification's "Fake/Inline implementation":

- record-only (default): tasks are captured for assertions and never executed, so a
  test can verify *that* work was enqueued without running it.
- inline: tasks are executed immediately through a dispatcher, so local development
  gets end-to-end behaviour with no Cloud Tasks emulator (which does not exist).

Inline execution is deliberately not a Cloud Tasks substitute — retries, backoff and
OIDC authentication are Phase 7 concerns and are absent here.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from app.core.errors import RetryableProviderError
from app.integrations.queue.provider import (
    EnqueuedTask,
    QueueProvider,
    TaskRequest,
    TaskType,
)

TaskDispatcher = Callable[[TaskRequest], Awaitable[None]]


class InMemoryQueueProvider(QueueProvider):
    name = "inline"

    def __init__(
        self,
        *,
        dispatcher: TaskDispatcher | None = None,
        execute_inline: bool = False,
        fail_with: Exception | None = None,
    ) -> None:
        if execute_inline and dispatcher is None:
            raise ValueError("execute_inline requires a dispatcher")
        self.enqueued: list[TaskRequest] = []
        self.executed: list[TaskRequest] = []
        self._dispatcher = dispatcher
        self.execute_inline = execute_inline
        self.fail_with = fail_with
        # Task IDs double as idempotency keys (§20); a repeat is not re-executed.
        self._seen_task_ids: set[str] = set()

    async def enqueue(self, task: TaskRequest) -> EnqueuedTask:
        if self.fail_with is not None:
            raise self.fail_with

        self.enqueued.append(task)

        if (
            self.execute_inline
            and self._dispatcher is not None
            and task.task_id not in self._seen_task_ids
        ):
            self._seen_task_ids.add(task.task_id)
            await self._dispatcher(task)
            self.executed.append(task)

        return EnqueuedTask(task_id=task.task_id, task_type=task.task_type)

    # ---------------------------------------------------------------- test helpers

    def set_dispatcher(self, dispatcher: TaskDispatcher, *, execute_inline: bool = True) -> None:
        self._dispatcher = dispatcher
        self.execute_inline = execute_inline

    @property
    def count(self) -> int:
        return len(self.enqueued)

    def tasks_of_type(self, task_type: TaskType) -> list[TaskRequest]:
        return [task for task in self.enqueued if task.task_type is task_type]

    def task_types(self) -> list[TaskType]:
        return [task.task_type for task in self.enqueued]

    def reset(self) -> None:
        self.enqueued.clear()
        self.executed.clear()
        self._seen_task_ids.clear()

    def fail_retryably(self, message: str = "simulated queue unavailability") -> None:
        self.fail_with = RetryableProviderError(message, provider=self.name)

    def stop_failing(self) -> None:
        self.fail_with = None
