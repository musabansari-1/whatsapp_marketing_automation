"""Failed-booking follow-up: the bell behind the fallback promise.

When a booking tool fails terminally — the calendar cannot answer, or the
product cannot book — the turn tells the customer a colleague will follow up.
That sentence is a promise, and this task is what keeps it: one dashboard
notification per active internal user, linked to the lead, so whoever sees the
bell first can open the transcript and arrange the demo by hand.

Team-only: the customer already heard the promise in the reply, and a second
message would double-text them (Phase 9: one reply per inbound message).

A taken slot is deliberately not this. The model offers another time there, so
there is nothing for the team to do; the tool reports ok=False without setting
the flag, and no bell rings.

Repeats collapse rather than re-announcing: the entity is the lead, so the
fan-out's unique constraint on (user, type, entity) makes a second failure for
the same lead insert nothing. A repeat failure months later on the same lead
likewise stays silent — the trade-off accepted for a bell the team does not
learn to ignore, and the transcript always holds the full story.
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


async def notify_booking_failed(task: TaskRequest) -> dict[str, int | str]:
    """Fan out the dashboard notification for a failed booking."""
    lead_id = task.payload.get("lead_id")
    reason = task.payload.get("reason")
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
            notification_type=NotificationType.BOOKING_FAILED,
            title=_notification_title(product),
            message=_notification_message(lead, str(reason or "")),
            entity_type=ENTITY_LEAD,
            entity_id=lead.id,
            product_id=lead.product_id,
        )
        await session.commit()

    logger.info("booking_failed_notifications_created", notified=notified, lead_id=str(lead_id))

    return {
        "result": "notified",
        "lead_id": str(lead_id),
        "notified": notified,
    }


def _notification_title(product: Product | None) -> str:
    """The bell's headline, naming the product ([D47]).

    Distinct from a booked demo's title on purpose: this one needs contacting,
    not joining.
    """
    return f"Booking failed — {product.display_name}" if product is not None else "Booking failed"


def _notification_message(lead: Lead, reason: str) -> str:
    """What a salesperson needs to act: who, how to reach them, and why.

    The transcript on the lead holds the full story; the reason here is the
    one-line triage (calendar down vs. product unconfigured).
    """
    who = lead.full_name or lead.phone_e164
    company = f" ({lead.company})" if lead.company else ""
    return (
        f"{who}{company} ({lead.phone_e164}) asked for a demo that could not be "
        f"booked: {reason} They were told a colleague will follow up — contact "
        "them to arrange it by hand."
    )


__all__ = ["notify_booking_failed"]
