"""Notification and booking endpoints (specification §13, §16, §17, [D3]).

Notifications are strictly per-user. Every route here resolves the user from the
session and never takes a user ID: one salesperson must not be able to read or clear
another's notifications by guessing an identifier.

Bookings are readable by any internal user. §9 restricts broadcasting, not reading, and
a salesperson about to run a demo needs to see it.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import CurrentUser, DbSession
from app.api.schemas import (
    BookingPage,
    BookingResponse,
    MarkAllReadResponse,
    NotificationPage,
    NotificationResponse,
)
from app.core.logging import get_logger
from app.domain.enums import BookingStatus
from app.persistence.repositories import bookings as booking_repo

router = APIRouter(tags=["notifications"])
logger = get_logger(__name__)

MAX_PAGE_SIZE = 200


@router.get("/notifications", response_model=NotificationPage)
async def list_notifications(
    user: CurrentUser,
    session: DbSession,
    unread_only: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
) -> NotificationPage:
    """This user's notifications.

    `unread` is returned alongside the page total because the bell badge shows it
    regardless of which page is being read.
    """
    total, unread, notifications = await booking_repo.list_notifications(
        session, user_id=user.id, unread_only=unread_only, limit=limit, offset=offset
    )
    return NotificationPage(
        total=total,
        unread=unread,
        limit=limit,
        offset=offset,
        items=[NotificationResponse.model_validate(row) for row in notifications],
    )


@router.post("/notifications/{notification_id}/read", response_model=NotificationResponse)
async def mark_notification_read(
    notification_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> NotificationResponse:
    """Mark one notification read. Idempotent — a double click is not an error."""
    notification = await booking_repo.get_notification(session, notification_id)

    # A notification belonging to someone else is reported as missing rather than
    # forbidden: confirming it exists would leak that another user was notified.
    if notification is None or notification.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found")

    await booking_repo.mark_read(session, notification)
    await session.commit()
    return NotificationResponse.model_validate(notification)


@router.post("/notifications/read-all", response_model=MarkAllReadResponse)
async def mark_all_notifications_read(user: CurrentUser, session: DbSession) -> MarkAllReadResponse:
    """Clear the badge. Only ever this user's own notifications."""
    updated = await booking_repo.mark_all_read(session, user.id)
    await session.commit()
    return MarkAllReadResponse(marked_read=updated)


@router.get("/bookings", response_model=BookingPage)
async def list_bookings(
    user: CurrentUser,
    session: DbSession,
    booking_status: BookingStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
) -> BookingPage:
    total, bookings = await booking_repo.list_bookings(
        session, status=booking_status, limit=limit, offset=offset
    )
    return BookingPage(
        total=total,
        limit=limit,
        offset=offset,
        items=[BookingResponse.model_validate(row) for row in bookings],
    )


@router.get("/bookings/{booking_id}", response_model=BookingResponse)
async def get_booking(
    booking_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> BookingResponse:
    booking = await booking_repo.get_booking(session, booking_id)
    if booking is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Booking not found")
    return BookingResponse.model_validate(booking)


__all__ = ["router"]
