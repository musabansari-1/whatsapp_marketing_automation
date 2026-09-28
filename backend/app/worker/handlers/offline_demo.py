"""Offline demo request follow-up ([D66]).

What happens *after* a customer chooses an offline demo instead of booking a time:
the team is told, and nothing else happens.

An offline request is deliberately not a booking: it carries no time, so there is no
calendar event to create and no `bookings` row to write (`start_time` is NOT NULL,
and a dummy time would be dishonest data that also blocks the diary). The request
lives as a fact on the lead (`offline_demo_requested`) plus this notification,
which is the team's work item.

Team-only, by the same rule that keeps the booking confirmation out of a second
sending path: the customer was already told in the turn's reply ("our team will
contact you"), and a second message would double-text them (Phase 9: one reply per
inbound message).

A repeated request collapses rather than re-announcing: the fan-out's unique
constraint on (user, type, entity) makes a second fan-out for the same lead insert
nothing, which the inserted count makes visible in logs.
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

logger = get_logger(__name__)

#: The notification entity for an offline request: the lead to contact, since there
#: is no booking to link to. The dashboard renders what the title and message say;
#: only a `booking` entity links to the demos list.
ENTITY_LEAD = "lead"


async def notify_offline_demo(task: TaskRequest) -> dict[str, int | str]:
    """Fan out the dashboard notification for an offline demo request."""
    lead_id = task.payload.get("lead_id")
    if not lead_id:
        raise ValueError("lead_id missing from task payload")

    factory = get_session_factory()

    async with factory() as session:
        lead = await conversation_repo.get_lead(session, uuid.UUID(str(lead_id)))
        if lead is None:
            # Nothing to retry against.
            raise ValueError(f"Lead {lead_id} not found")

        product = await session.get(Product, lead.product_id)
        notified = await booking_repo.fan_out_notification(
            session,
            notification_type=NotificationType.DEMO_BOOKED,
            title=_notification_title(product),
            message=_notification_message(lead),
            entity_type=ENTITY_LEAD,
            entity_id=lead.id,
            product_id=lead.product_id,
        )
        await session.commit()

    logger.info("offline_demo_notifications_created", notified=notified, lead_id=str(lead_id))

    return {
        "result": "notified",
        "lead_id": str(lead_id),
        "notified": notified,
    }


def _notification_title(product: Product | None) -> str:
    """The bell's headline, naming the product ([D47]) and the mode.

    Distinct from a booked demo's title on purpose: the team must see at a glance
    that this one needs contacting, not joining.
    """
    return (
        f"Offline demo requested — {product.display_name}"
        if product is not None
        else "Offline demo requested"
    )


def _notification_message(lead: Lead) -> str:
    """What a salesperson needs to act: who, how to reach them, and what is missing.

    No time and no link exist by design ([D66]): the venue and the time are what the
    contact is for.
    """
    who = lead.full_name or lead.phone_e164
    company = f" ({lead.company})" if lead.company else ""
    return (
        f"{who}{company} ({lead.phone_e164}) asked for an offline demo. "
        "Contact them to arrange a venue and time — no calendar event was created."
    )


__all__ = ["ENTITY_LEAD", "notify_offline_demo"]
