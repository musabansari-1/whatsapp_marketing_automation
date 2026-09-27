"""Booking follow-up (specification §12, §13, §20, [D3], [D21]).

What happens *after* a demo is confirmed: tell the team, and tell the customer.

Both are deliberately out of the booking path itself. §12 is explicit that a confirmed
booking stays confirmed while a failed notification retries — so notifying cannot be
allowed to undo, delay, or cast doubt on a booking Google has already accepted. Running
it as its own task gives it its own retries without touching the booking.

The order within the task follows §12: dashboard notification first, customer
confirmation second. The team knowing about a demo matters more than the customer's
acknowledgement being instant, and if the confirmation fails the team can still act.

V1 notifies salespeople by dashboard notification and calendar entry only. No email, no
WhatsApp to the team — that is a scope rule, not an omission.
"""

from __future__ import annotations

import uuid

from app.core.config import Settings, get_settings
from app.core.correlation import bind_log_context
from app.core.errors import ProviderError
from app.core.logging import get_logger
from app.domain.enums import NotificationType
from app.domain.retry import RetryPolicy, backoff_seconds, classify, should_retry
from app.integrations.queue import TaskRequest
from app.integrations.registry import WhatsAppNotConfiguredError, build_whatsapp_provider
from app.integrations.whatsapp import OutboundTextMessage
from app.persistence.database import get_session_factory
from app.persistence.models import Booking, Lead, Product
from app.persistence.repositories import bookings as booking_repo
from app.persistence.repositories import conversations as conversation_repo
from app.worker.handlers.campaign_send import RetryableSendError
from app.worker.handlers.crm_sync import queue_crm_sync

logger = get_logger(__name__)


def confirmation_idempotency_key(booking_id: str) -> str:
    """One confirmation per booking, by construction.

    Derived from the booking rather than generated, so a retried task computes the
    same key and the unique constraint on `messages.idempotency_key` refuses the
    second write — the customer is not told twice.
    """
    return f"booking-confirmation-{booking_id}"


async def notify_booking(task: TaskRequest) -> dict[str, int | str]:
    """Fan out the dashboard notification and send the customer's confirmation."""
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

        phone = lead.phone_e164
        lead_id = lead.id
        confirmation = _confirmation_message(settings, booking)
        conversation_id = booking.conversation_id
        product_id = booking.product_id

    logger.info("booking_notifications_created", notified=notified)

    # A booked demo is exactly what a salesperson expects to see in the CRM. Enqueued
    # rather than written here, and best-effort: §14 forbids a CRM failure affecting a
    # confirmed booking.
    await queue_crm_sync(settings, lead_id)

    sent = await _send_confirmation(
        settings,
        booking_id=str(booking_id),
        phone=phone,
        body=confirmation,
        conversation_id=conversation_id,
        product_id=product_id,
    )

    return {
        "result": "notified",
        "booking_id": str(booking_id),
        "notified": notified,
        "confirmed": int(sent),
    }


# ---------------------------------------------------------------- internals


async def _send_confirmation(
    settings: Settings,
    *,
    booking_id: str,
    phone: str,
    body: str,
    conversation_id: uuid.UUID | None,
    product_id: uuid.UUID,
) -> bool:
    """Tell the customer their demo is booked.

    A free-form message, which is permitted because this is a reply inside the
    24-hour window the customer opened by asking for the demo. No template is needed,
    which is why this does not depend on [D24].
    """
    factory = get_session_factory()
    idempotency_key = confirmation_idempotency_key(booking_id)

    async with factory() as session:
        if await conversation_repo.find_reply_by_idempotency_key(session, idempotency_key):
            # Already sent, by an earlier attempt of this task.
            logger.info("booking_confirmation_already_sent")
            return False

        # The confirmation leaves from the product's own number ([D40]) — the one the
        # conversation ran on, so it lands in that thread. Taken from the booking, which
        # carries its product since Phase 24, rather than through the conversation: that
        # link is SET NULL on delete, and losing it used to fall back to the platform
        # number, reaching the customer from one they had never seen.
        product = await session.get(Product, product_id)

    try:
        if product is None:  # pragma: no cover - bookings.product_id is a NOT NULL FK
            raise WhatsAppNotConfiguredError(f"Product {product_id} no longer exists")
        whatsapp = build_whatsapp_provider(settings, product=product)
    except WhatsAppNotConfiguredError as exc:
        # Permanent until a number is configured, and no other number may stand in. The
        # booking stands and the team has been notified, as for any failed confirmation.
        logger.warning("booking_confirmation_failed", error=str(exc))
        return False

    try:
        result = await whatsapp.send_text_message(
            OutboundTextMessage(to_e164=phone, body=body, idempotency_key=idempotency_key)
        )
    except ProviderError as exc:
        _raise_if_retryable(settings, exc)
        # Permanent: the booking stands and the team has been notified, so this is
        # logged rather than failing the task and re-running the fan-out forever.
        logger.warning("booking_confirmation_failed", error=str(exc))
        return False

    async with factory() as session:
        booking = await booking_repo.get_booking(session, uuid.UUID(booking_id))
        conversation = (
            await conversation_repo.get_conversation(session, conversation_id)
            if conversation_id
            else None
        )
        lead = await conversation_repo.get_lead(session, booking.lead_id) if booking else None

        if booking is not None and lead is not None and conversation is not None:
            from datetime import UTC, datetime

            from app.domain.enums import MessageStatus

            message = await conversation_repo.record_outbound_reply(
                session,
                lead=lead,
                conversation=conversation,
                body=body,
                idempotency_key=idempotency_key,
                provider_message_id=result.provider_message_id,
                status=MessageStatus.SENT,
                sent_at=datetime.now(UTC),
            )
            # The transcript stays complete: the AI's next turn should know the
            # customer was already told, or it will say it again.
            await conversation_repo.record_turn(
                session,
                conversation=conversation,
                role=conversation_repo.ROLE_ASSISTANT,
                content=body,
                message_id=message.id,
            )
            await conversation_repo.touch_outbound(session, conversation)
            await session.commit()

    logger.info("booking_confirmation_sent")
    return True


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


def _confirmation_message(settings: Settings, booking: Booking) -> str:
    """The customer's confirmation.

    Configurable copy ([D21]). The template is formatted defensively: an operator
    editing it must not be able to break booking confirmations with a stray brace.
    When Meet is missing, fall back to time-only copy so we never send
    "Join here: " with an empty URL.
    """
    local = _local_time(booking)
    if not booking.meeting_link:
        return f"Your demo is confirmed for {local} ({booking.timezone})."
    try:
        return settings.msg_booking_confirmation.format(
            start_time=local,
            timezone=booking.timezone,
            meeting_link=booking.meeting_link,
        )
    except (KeyError, IndexError, ValueError):
        logger.warning("booking_confirmation_template_invalid")
        return (
            f"Your demo is confirmed for {local} ({booking.timezone}). "
            f"Join here: {booking.meeting_link}"
        )


def _local_time(booking: Booking) -> str:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        zone = ZoneInfo(booking.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return booking.start_time.strftime("%A %d %B %Y, %I:%M %p UTC")

    return booking.start_time.astimezone(zone).strftime("%A %d %B %Y, %I:%M %p")


def _raise_if_retryable(settings: Settings, exc: ProviderError) -> None:
    policy = RetryPolicy(
        max_attempts=settings.send_max_attempts,
        base_seconds=settings.send_backoff_base_seconds,
        max_seconds=settings.send_backoff_max_seconds,
        jitter=settings.send_backoff_jitter,
    )
    failure = classify(exc)
    if should_retry(failure=failure, attempts_made=1, policy=policy):
        raise RetryableSendError(
            str(exc), retry_after_seconds=backoff_seconds(attempts_made=1, policy=policy)
        ) from exc


__all__ = ["confirmation_idempotency_key", "notify_booking"]
