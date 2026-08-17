"""Task handler registry.

The seam between the queue and the code that does the work. Phase 1 establishes it
with no handlers registered; each later phase registers the tasks it owns.

Retry policy, Cloud Tasks OIDC authentication and idempotency bookkeeping are Phase 7
and are deliberately absent here.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from app.integrations.queue import TaskRequest, TaskType

TaskHandler = Callable[[TaskRequest], Awaitable[Any]]


class UnknownTaskTypeError(LookupError):
    """No handler is registered for the task type."""

    def __init__(self, task_type: str) -> None:
        super().__init__(f"No handler registered for task type '{task_type}'")
        self.task_type = task_type


class TaskRegistry:
    def __init__(self) -> None:
        self._handlers: dict[TaskType, TaskHandler] = {}

    def register(self, task_type: TaskType, handler: TaskHandler) -> None:
        if task_type in self._handlers:
            raise ValueError(f"Handler for '{task_type.value}' is already registered")
        self._handlers[task_type] = handler

    def get(self, task_type: TaskType) -> TaskHandler:
        handler = self._handlers.get(task_type)
        if handler is None:
            raise UnknownTaskTypeError(task_type.value)
        return handler

    def registered_types(self) -> list[TaskType]:
        return sorted(self._handlers, key=lambda task_type: task_type.value)

    def clear(self) -> None:
        self._handlers.clear()


task_registry = TaskRegistry()
