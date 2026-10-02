"""Booking follow-up (specification §12, §13, §20, [D3], [D21]).

What happens *after* a demo is confirmed: tell the team.

The turn's reply is the customer's confirmation — it carries the time and the
join link, and the prompts require it to. A second message from this task would
double-text the customer about a booking they were just told of (one reply per
inbound message), so this task is team-only: the dashboard bell, plus the CRM
enqueue the sales record needs.

Kept out of the booking path itself all the same. §12 is explicit that a confirmed
booking stays confirmed while a failed notification retries — so notifying cannot be
allowed to undo, delay, or cast doubt on a booking Google has already accepted. Running
it as its own task gives it its own retries without touching the booking.

V1 notifies salespeople by dashboard notification and calendar entry only. No email, no
WhatsApp to the team — that is a scope rule, not an omission.
"""

from __future__ import annotations

import uuid

from app.core.config import get_settings
from app.core.correlation import bind_log_context
from app.core.logging import get_logger
from app.domain.enums import NotificationType
from app.integrations.queue import TaskRequest
from app.persistence.database import get_session_factory
from app.persistence.models import Booking, Lead, Product
from app.persistence.repositories import bookings as booking_repo
from app.persistence.repositories import conversations as conversation_repo
from app.worker.handlers.crm_sync import queue_crm_sync

logger = get_logger(__name__)


async def notify_booking(task: TaskRequest) -> dict[str, int | str]:
    """Fan out the dashboard notification for a confirmed booking.

    Team-only: the turn's reply already confirmed the time and join link, and a
    second message would double-text the customer.
    """
    settings = get_settings()

    booking_id = task.payload.get("booking_id")
    if not booking_id:
        raise ValueError("booking_id missing from task payload")

    factory = get_session_factory()

    async with factory() as session:
        booking = await booking_repo.get_booking(session, uuid.UUID(str(booking_id)))
        if booking is None:
            # Nothing to retry against.
            raise ValueError(f"Booking {booking_id} not found")

        lead = await conversation_repo.get_lead(session, booking.lead_id)
        if lead is None:
            raise ValueError(f"Lead for booking {booking_id} not found")

        bind_log_context(booking_id=str(booking.id), lead_id=str(lead.id))

        # [D47]: a notification about a booking names its product. The whole team still
        # hears about every booking ([D3]), so the bell has to tell a SustainX presentation
        # from an Academicae demo without anyone opening it.
        product = await session.get(Product, booking.product_id)
        notified = await booking_repo.fan_out_notification(
            session,
            notification_type=NotificationType.DEMO_BOOKED,
            title=_notification_title(product),
            message=_notification_message(booking, lead),
            entity_type=booking_repo.ENTITY_BOOKING,
            entity_id=booking.id,
            product_id=booking.product_id,
        )
        await session.commit()

        lead_id = lead.id

    logger.info("booking_notifications_created", notified=notified)

    # A booked demo is exactly what a salesperson expects to see in the CRM. Enqueued
    # rather than written here, and best-effort: §14 forbids a CRM failure affecting a
    # confirmed booking.
    await queue_crm_sync(settings, lead_id)

    return {
        "result": "notified",
        "booking_id": str(booking_id),
        "notified": notified,
    }


# ---------------------------------------------------------------- internals


def _notification_title(product: Product | None) -> str:
    """The bell's headline, naming the product ([D47]).

    The display name is data, so a renamed product reads correctly with no code change.
    `None` cannot happen — `bookings.product_id` is a NOT NULL foreign key — and is
    handled only so the type is honest.
    """
    return f"Demo booked — {product.display_name}" if product is not None else "Demo booked"


def _notification_message(booking: Booking, lead: Lead) -> str:
    """What a salesperson sees on the dashboard.

    Rendered in the booking's own timezone: the sales team reads this, and a UTC
    timestamp would need translating in their head at exactly the wrong moment.
    """
    when = _local_time(booking)
    who = lead.full_name or lead.phone_e164
    company = f" ({lead.company})" if lead.company else ""
    message = f"{who}{company} booked a demo for {when}."
    if booking.meeting_link:
        message = f"{message} Meet: {booking.meeting_link}"
    return message


def _local_time(booking: Booking) -> str:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        zone = ZoneInfo(booking.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return booking.start_time.strftime("%A %d %B %Y, %I:%M %p UTC")

    return booking.start_time.astimezone(zone).strftime("%A %d %B %Y, %I:%M %p")


__all__ = ["notify_booking"]
