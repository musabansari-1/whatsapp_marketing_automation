"""Queue provider interface (specification §18).

    QueueProvider
     ├── CloudTasksProvider  (Phase 7)
     └── InMemoryQueueProvider

Created in Phase 1 on purpose: specification §8 requires webhooks to persist work and
return quickly, and Phase 6 lands before the real Cloud Tasks integration in Phase 7.
The abstraction lets Phase 6 enqueue against a working queue immediately.

Each task carries a `task_id` used both as the correlation identifier (§21) and as the
idempotency key (§20), so a redelivered task is recognizable as a repeat.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class TaskType(StrEnum):
    """Task kinds the platform will enqueue (§18).

    Declared up front so the queue contract is complete; handlers are registered by
    the phase that owns each one.
    """

    SEND_CAMPAIGN_MESSAGE = "send_campaign_message"
    PROCESS_INBOUND_MESSAGE = "process_inbound_message"
    INGEST_KNOWLEDGE_DOCUMENT = "ingest_knowledge_document"
    GENERATE_EMBEDDINGS = "generate_embeddings"
    SYNC_CRM_CONTACT = "sync_crm_contact"
    SYNC_WHATSAPP_TEMPLATES = "sync_whatsapp_templates"


@dataclass(frozen=True, slots=True)
class TaskRequest:
    """A unit of background work.

    `payload` must be JSON-serializable: the real transport moves it over HTTP.
    """

    task_type: TaskType
    payload: dict[str, Any] = field(default_factory=dict)
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    correlation_id: str | None = None
    scheduled_for: datetime | None = None

    def __post_init__(self) -> None:
        if self.scheduled_for is not None and self.scheduled_for.tzinfo is None:
            raise ValueError("scheduled_for must be timezone-aware")


@dataclass(frozen=True, slots=True)
class EnqueuedTask:
    task_id: str
    task_type: TaskType
    provider_task_name: str | None = None


class QueueProvider(ABC):
    name: str

    @abstractmethod
    async def enqueue(self, task: TaskRequest) -> EnqueuedTask:
        """Hand the task to the queue and return promptly.

        Callers must never wait for the work itself: §4 forbids long-running work
        inside HTTP requests and webhooks.
        """
