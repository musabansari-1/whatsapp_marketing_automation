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
    # One customer turn, queued per stored inbound message. Distinct from
    # PROCESS_INBOUND_MESSAGE, which handles a whole webhook delivery: one webhook can
    # carry several messages, and each needs its own retryable, idempotent unit of work.
    PROCESS_CONVERSATION_TURN = "process_conversation_turn"
    # Telling the team and the customer about a confirmed booking. Its own task
    # because §12 says a confirmed booking stays confirmed while a failed
    # notification retries — so notifying must never be able to undo or delay it.
    NOTIFY_BOOKING = "notify_booking"
    # Telling the team about an offline demo request ([D66]). Its own task for the
    # same reason as NOTIFY_BOOKING, but team-only: the customer was already told
    # in the turn's reply, and a second message would double-text them.
    NOTIFY_OFFLINE_DEMO = "notify_offline_demo"
    # Telling the team a booking failed terminally, after the customer was told a
    # colleague will follow up. Without this the promise in the fallback reply is
    # hollow: the customer waits and nobody is told. Team-only, same reason.
    NOTIFY_BOOKING_FAILED = "notify_booking_failed"
    # Telling the team a lead wants to register or asked for a person. Team-only:
    # the turn's reply already answered the customer.
    NOTIFY_LEAD_ATTENTION = "notify_lead_attention"
    # A team member's reply typed in the dashboard. Sent by the worker, not the API,
    # because the worker is where the window and suppression checks live.
    SEND_TEAM_REPLY = "send_team_reply"
    # An approved template another system asked for (a status update such as "your
    # profile is approved"). Sent by the worker for the same reason as a team reply.
    SEND_TEMPLATE_NOTIFICATION = "send_template_notification"
    INGEST_KNOWLEDGE_DOCUMENT = "ingest_knowledge_document"
    GENERATE_EMBEDDINGS = "generate_embeddings"
    SYNC_CRM_CONTACT = "sync_crm_contact"
    SYNC_WHATSAPP_TEMPLATES = "sync_whatsapp_templates"
    # Recovery sweep for inbound messages whose conversation turn was never queued
    # (§20). Scheduled rather than event-driven, because the failure it repairs is the
    # absence of an event.
    SWEEP_UNANSWERED_MESSAGES = "sweep_unanswered_messages"
    # Housekeeping for the §19 rate limit counters.
    SWEEP_RATE_LIMIT_COUNTERS = "sweep_rate_limit_counters"
    # Housekeeping for expired session rows (D-43). Expiry is already enforced when a
    # session is read, so this is growth control, not access control.
    SWEEP_EXPIRED_SESSIONS = "sweep_expired_sessions"
    # Bot-activity audit ([D67]). Scores recent conversations for automated-looking
    # inbound patterns and flags suspects for human review. Detection only: flagging
    # never gates a reply, so this task sends nothing and changes no state except
    # the notification itself.
    SWEEP_BOT_ACTIVITY = "sweep_bot_activity"


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
