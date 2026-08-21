"""Task handlers, registered with the worker's dispatch registry.

Each phase registers the tasks it owns. Registration happens here so the worker
service picks them up on import, and so a phase adding a handler does not have to
touch the worker entrypoint.
"""

from app.integrations.queue import TaskType
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
    }

    for task_type, handler in handlers.items():
        if task_type not in registered:
            target.register(task_type, handler)


__all__ = ["process_inbound_webhook", "register_all", "sync_whatsapp_templates"]
