"""Booking and notification persistence (specification §12, §13, [D3]).

Two invariants carry this module.

**A booking is recorded once per calendar event.** `calendar_event_id` is unique, so a
retried booking converges on the existing row instead of adding a second. The recovery
path matters more than it looks: the calendar event is created *before* the row is
written, so a crash in between leaves an event with no record — and the next attempt
must find it rather than book again.

**The notification fan-out is idempotent.** [D3] gives every active internal user a
notification per booking, and §12 says a confirmed booking stays confirmed while a
failed notification retries. The unique constraint on
`(user_id, type, related_entity_type, related_entity_id)` is what lets the retry re-run
for everyone without duplicating for the users it already reached.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import BookingStatus, NotificationType, UserRole
from app.integrations.calendar import CalendarEvent
from app.persistence.models import Booking, Lead, Notification, User

# §13 relates a notification to what it is about, so the dashboard can link to it.
ENTITY_BOOKING = "booking"


# ---------------------------------------------------------------- bookings


async def get_booking(session: AsyncSession, booking_id: uuid.UUID) -> Booking | None:
    return await session.get(Booking, booking_id)


async def find_by_event_id(session: AsyncSession, calendar_event_id: str) -> Booking | None:
    """The booking recorded for a calendar event, if any.

    This is the recovery path: a retry that finds its own earlier event here knows the
    booking is already recorded and must not create another.
    """
    result = await session.execute(
        select(Booking).where(Booking.calendar_event_id == calendar_event_id)
    )
    return result.scalars().first()


async def find_confirmed_at(session: AsyncSession, start_time: datetime) -> Booking | None:
    """A confirmed booking already occupying this slot.

    Only CONFIRMED counts: a cancelled or failed attempt must not block rebooking the
    same time, which is why the unique index is partial.
    """
    result = await session.execute(
        select(Booking).where(
            Booking.start_time == start_time,
            Booking.status == BookingStatus.CONFIRMED,
        )
    )
    return result.scalars().first()


async def record_confirmed_booking(
    session: AsyncSession,
    *,
    lead: Lead,
    conversation_id: uuid.UUID | None,
    event: CalendarEvent,
) -> tuple[Booking, bool]:
    """Record a booking Google has confirmed, or return the existing one.

    Returns the booking and whether it was newly created, so the caller can skip the
    side effects — the notification fan-out and the customer's confirmation — for a
    booking it has already handled.

    CONFIRMED, not PENDING: this is only called once the calendar has returned an
    event, which is exactly what §12 requires before a booking may be treated as real.
    """
    existing = await find_by_event_id(session, event.event_id)
    if existing is not None:
        return existing, False

    booking = Booking(
        lead_id=lead.id,
        conversation_id=conversation_id,
        calendar_event_id=event.event_id,
        start_time=event.interval.start,
        end_time=event.interval.end,
        # Stored per booking so a later policy change cannot retroactively
        # reinterpret this one ([D8]).
        timezone=event.timezone,
        status=BookingStatus.CONFIRMED,
        meeting_link=event.meeting_link,
        customer_email=lead.email,
        confirmed_at=datetime.now(UTC),
    )
    session.add(booking)
    await session.flush()

    return booking, True


async def mark_cancelled(session: AsyncSession, booking: Booking) -> None:
    booking.status = BookingStatus.CANCELLED
    booking.cancelled_at = datetime.now(UTC)
    await session.flush()


async def list_bookings(
    session: AsyncSession,
    *,
    status: BookingStatus | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[int, Sequence[Booking]]:
    """A page of bookings, soonest first."""
    conditions = [] if status is None else [Booking.status == status]

    total = await session.scalar(select(func.count()).select_from(Booking).where(*conditions))
    result = await session.execute(
        select(Booking)
        .where(*conditions)
        .order_by(Booking.start_time.desc())
        .limit(limit)
        .offset(offset)
    )
    return int(total or 0), result.scalars().all()


# ---------------------------------------------------------------- notifications


async def active_internal_user_ids(session: AsyncSession) -> list[uuid.UUID]:
    """Everyone who should hear about a booking ([D3]).

    Every active user, both roles: V1 has no lead assignment, so there is nobody in
    particular to notify and the whole team is told.
    """
    result = await session.execute(
        select(User.id).where(
            User.is_active.is_(True),
            User.role.in_([UserRole.ADMIN, UserRole.SALESPERSON]),
        )
    )
    return list(result.scalars().all())


async def fan_out_notification(
    session: AsyncSession,
    *,
    notification_type: NotificationType,
    title: str,
    message: str,
    entity_type: str,
    entity_id: uuid.UUID,
) -> int:
    """Give every active internal user one notification, at most once each.

    Written as a single INSERT ... ON CONFLICT DO NOTHING rather than a read-then-write
    loop. Two reasons: a retry after a partial failure re-runs for everyone and adds
    nothing for the users it already reached, and two workers racing the same booking
    cannot both insert.

    Returns how many rows were actually inserted, which is what makes the retry's
    "nothing more to do" visible in logs.
    """
    user_ids = await active_internal_user_ids(session)
    if not user_ids:
        # Worth seeing: a booking nobody hears about usually means every user was
        # deactivated, not that the fan-out is broken.
        return 0

    statement = (
        pg_insert(Notification)
        .values(
            [
                {
                    "user_id": user_id,
                    "type": notification_type,
                    "title": title,
                    "message": message,
                    "related_entity_type": entity_type,
                    "related_entity_id": entity_id,
                }
                for user_id in user_ids
            ]
        )
        .on_conflict_do_nothing(constraint="uq_notifications_user_type_entity")
    )

    # RETURNING rather than rowcount: the typed Result exposes rows, and counting the
    # rows actually inserted is what makes a retry's "nothing more to do" visible.
    result = await session.execute(statement.returning(Notification.id))
    inserted = len(list(result.scalars().all()))
    await session.flush()
    return inserted


async def list_notifications(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    unread_only: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> tuple[int, int, Sequence[Notification]]:
    """One user's notifications, newest first, with totals.

    Returns the matching total and the unread count. The unread count is separate
    because the bell badge shows it regardless of which page is being viewed.
    """
    conditions = [Notification.user_id == user_id]
    if unread_only:
        conditions.append(Notification.is_read.is_(False))

    total = await session.scalar(select(func.count()).select_from(Notification).where(*conditions))
    unread = await session.scalar(
        select(func.count())
        .select_from(Notification)
        .where(Notification.user_id == user_id, Notification.is_read.is_(False))
    )
    result = await session.execute(
        select(Notification)
        .where(*conditions)
        .order_by(Notification.created_at.desc())
        .limit(limit)
        .offset(offset)
    )

    return int(total or 0), int(unread or 0), result.scalars().all()


async def get_notification(
    session: AsyncSession, notification_id: uuid.UUID
) -> Notification | None:
    return await session.get(Notification, notification_id)


async def mark_read(session: AsyncSession, notification: Notification) -> None:
    """Idempotent: `read_at` keeps its original value on a second call."""
    if not notification.is_read:
        notification.is_read = True
        notification.read_at = datetime.now(UTC)
        await session.flush()


async def mark_all_read(session: AsyncSession, user_id: uuid.UUID) -> int:
    """Mark every unread notification read. Returns how many changed."""
    result = await session.execute(
        select(Notification).where(Notification.user_id == user_id, Notification.is_read.is_(False))
    )
    notifications = list(result.scalars().all())

    now = datetime.now(UTC)
    for notification in notifications:
        notification.is_read = True
        notification.read_at = now

    await session.flush()
    return len(notifications)


__all__ = [
    "ENTITY_BOOKING",
    "active_internal_user_ids",
    "fan_out_notification",
    "find_by_event_id",
    "find_confirmed_at",
    "get_booking",
    "get_notification",
    "list_bookings",
    "list_notifications",
    "mark_all_read",
    "mark_cancelled",
    "mark_read",
    "record_confirmed_booking",
]
