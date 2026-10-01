"""Task handlers, registered with the worker's dispatch registry.

Each phase registers the tasks it owns. Registration happens here so the worker
service picks them up on import, and so a phase adding a handler does not have to
touch the worker entrypoint.
"""

from app.integrations.queue import TaskType
from app.worker.handlers.booking import notify_booking
from app.worker.handlers.booking_failed import notify_booking_failed
from app.worker.handlers.bot_audit import sweep_bot_activity
from app.worker.handlers.campaign_send import send_campaign_message
from app.worker.handlers.conversation import process_conversation_turn
from app.worker.handlers.crm_sync import queue_crm_sync, sync_crm_contact
from app.worker.handlers.knowledge_ingest import ingest_knowledge_document
from app.worker.handlers.offline_demo import notify_offline_demo
from app.worker.handlers.sweeps import (
    sweep_expired_sessions,
    sweep_rate_limit_counters,
    sweep_unanswered_messages,
)
from app.worker.handlers.whatsapp import process_inbound_webhook, sync_whatsapp_templates
from app.worker.registry import TaskRegistry, task_registry


def register_all(registry: TaskRegistry | None = None) -> None:
    """Register every handler implemented so far.

    Idempotent: registering twice would raise, so an already-registered type is
    skipped. That matters because both the worker entrypoint and the tests call this.
    """
    target = registry or task_registry
    registered = set(target.registered_types())

    handlers = {
        TaskType.PROCESS_INBOUND_MESSAGE: process_inbound_webhook,
        TaskType.SYNC_WHATSAPP_TEMPLATES: sync_whatsapp_templates,
        TaskType.SEND_CAMPAIGN_MESSAGE: send_campaign_message,
        TaskType.INGEST_KNOWLEDGE_DOCUMENT: ingest_knowledge_document,
        TaskType.PROCESS_CONVERSATION_TURN: process_conversation_turn,
        TaskType.NOTIFY_BOOKING: notify_booking,
        TaskType.NOTIFY_BOOKING_FAILED: notify_booking_failed,
        TaskType.NOTIFY_OFFLINE_DEMO: notify_offline_demo,
        TaskType.SWEEP_BOT_ACTIVITY: sweep_bot_activity,
        TaskType.SYNC_CRM_CONTACT: sync_crm_contact,
        TaskType.SWEEP_UNANSWERED_MESSAGES: sweep_unanswered_messages,
        TaskType.SWEEP_RATE_LIMIT_COUNTERS: sweep_rate_limit_counters,
        TaskType.SWEEP_EXPIRED_SESSIONS: sweep_expired_sessions,
    }

    for task_type, handler in handlers.items():
        if task_type not in registered:
            target.register(task_type, handler)


__all__ = [
    "ingest_knowledge_document",
    "notify_booking",
    "notify_booking_failed",
    "notify_offline_demo",
    "queue_crm_sync",
    "process_conversation_turn",
    "process_inbound_webhook",
    "register_all",
    "send_campaign_message",
    "sweep_bot_activity",
    "sweep_expired_sessions",
    "sweep_rate_limit_counters",
    "sweep_unanswered_messages",
    "sync_crm_contact",
    "sync_whatsapp_templates",
]
