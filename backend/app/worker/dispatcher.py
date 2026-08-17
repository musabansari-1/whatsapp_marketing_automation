"""Task dispatch.

Each execution runs inside its own correlation scope so the task's identifiers
appear on every log line it produces and cannot leak into the next task handled by
the same process (§21).
"""

from __future__ import annotations

from typing import Any

from app.core.correlation import correlation_scope
from app.core.logging import get_logger
from app.integrations.queue import TaskRequest
from app.worker.registry import TaskRegistry, task_registry

logger = get_logger(__name__)


async def dispatch_task(task: TaskRequest, registry: TaskRegistry | None = None) -> Any:
    """Run the handler registered for `task`.

    Raises `UnknownTaskTypeError` when nothing is registered, which the HTTP layer
    turns into a non-retryable response — redelivering a task no handler claims would
    retry forever.
    """
    registry = registry or task_registry

    with correlation_scope(task.correlation_id, task_id=task.task_id):
        handler = registry.get(task.task_type)
        logger.info("task_started", task_type=task.task_type.value)
        try:
            result = await handler(task)
        except Exception:
            logger.exception("task_failed", task_type=task.task_type.value)
            raise
        logger.info("task_completed", task_type=task.task_type.value)
        return result
