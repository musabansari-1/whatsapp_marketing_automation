"""Hot-lead bell: a lead wants to register, or asked for a person.

An external-registration product (Life Herba) never books a demo, so none of the
booking bells ever ring for it, and its hottest leads (a doctor saying "how do I
join?") reached nobody. The assistant also tells a customer who asks for a person
that the team will reply in this chat; this bell is what makes that true.

Team-only: the turn's reply already answered the customer. Repeats collapse the
same way as the failed-booking bell: the entity is the lead, so the fan-out's
unique constraint on (user, type, entity) inserts nothing for a second reason on
the same lead. The transcript holds the rest.
"""

from __future__ import annotations

import uuid

from app.core.logging import get_logger
from app.domain.enums import NotificationType
from app.integrations.queue import TaskRequest
from app.persistence.database import get_session_factory
from app.persistence.models import Lead, Product
from app.persistence.repositories import bookings as booking_repo
from app.persistence.repositories import conversations as conversation_repo
from app.persistence.repositories.bookings import ENTITY_LEAD

logger = get_logger(__name__)


async def notify_lead_attention(task: TaskRequest) -> dict[str, int | str]:
    """Fan out the dashboard notification for a lead that needs a person."""
    lead_id = task.payload.get("lead_id")
    reason = str(task.payload.get("reason") or "needs attention")
    if not lead_id:
        raise ValueError("lead_id missing from task payload")

    factory = get_session_factory()

    async with factory() as session:
        lead = await conversation_repo.get_lead(session, uuid.UUID(str(lead_id)))
        if lead is None:
            raise ValueError(f"Lead {lead_id} not found")

        product = await session.get(Product, lead.product_id)
        notified = await booking_repo.fan_out_notification(
            session,
            notification_type=NotificationType.LEAD_NEEDS_ATTENTION,
            title=_notification_title(product, reason),
            message=_notification_message(lead, reason),
            entity_type=ENTITY_LEAD,
            entity_id=lead.id,
            product_id=lead.product_id,
        )
        await session.commit()

    logger.info("lead_attention_notifications_created", notified=notified, lead_id=str(lead_id))
    return {"result": "notified", "lead_id": str(lead_id), "notified": notified}


def _notification_title(product: Product | None, reason: str) -> str:
    headline = "Asked for a person" if "person" in reason else "Ready to register"
    return f"{headline} — {product.display_name}" if product is not None else headline


def _notification_message(lead: Lead, reason: str) -> str:
    who = lead.full_name or lead.phone_e164
    company = f" ({lead.company})" if lead.company else ""
    return (
        f"{who}{company} ({lead.phone_e164}) {reason}. Open the conversation to reply "
        "from the dashboard while the chat window is open."
    )


__all__ = ["notify_lead_attention"]
