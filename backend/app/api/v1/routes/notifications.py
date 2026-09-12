"""Notification endpoints (specification §13, §16, §17, [D3], [D47]).

Notifications are strictly per-user. Every route here resolves the user from the session
and never takes a user ID: one salesperson must not be able to read or clear another's
notifications by guessing an identifier.

Global rather than product-scoped, because a person has one inbox. Every active internal
user is still told about every booking ([D3]); what changed in Phase 26 is that each
notification carries its product, so the inbox can say which product a demo is for and
can be narrowed to one product when that helps.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import CurrentUser, DbSession
from app.api.schemas import (
    MarkAllReadResponse,
    NotificationPage,
    NotificationResponse,
    ProductRef,
)
from app.core.logging import get_logger
from app.persistence.models import Notification
from app.persistence.repositories import bookings as booking_repo
from app.persistence.repositories import products as product_repo

router = APIRouter(tags=["notifications"])
logger = get_logger(__name__)

MAX_PAGE_SIZE = 200


@router.get("/notifications", response_model=NotificationPage)
async def list_notifications(
    user: CurrentUser,
    session: DbSession,
    unread_only: bool = Query(default=False),
    product: str | None = Query(
        default=None, max_length=64, description="A product slug. Omit for every product."
    ),
    limit: int = Query(default=50, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
) -> NotificationPage:
    """This user's notifications, each naming its product.

    `product` narrows the page. `unread` is deliberately *not* narrowed: it drives the
    bell badge, which counts everything waiting for this person whichever product view
    they are in.
    """
    product_id: uuid.UUID | None = None
    if product:
        found = await product_repo.get_by_slug(session, product)
        if found is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
        product_id = found.id

    total, unread, notifications = await booking_repo.list_notifications(
        session,
        user_id=user.id,
        unread_only=unread_only,
        product_id=product_id,
        limit=limit,
        offset=offset,
    )
    return NotificationPage(
        total=total,
        unread=unread,
        limit=limit,
        offset=offset,
        items=await _with_products(session, notifications),
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
    [response] = await _with_products(session, [notification])
    return response


@router.post("/notifications/read-all", response_model=MarkAllReadResponse)
async def mark_all_notifications_read(user: CurrentUser, session: DbSession) -> MarkAllReadResponse:
    """Clear the badge. Only ever this user's own notifications, across every product."""
    updated = await booking_repo.mark_all_read(session, user.id)
    await session.commit()
    return MarkAllReadResponse(marked_read=updated)


async def _with_products(
    session: DbSession, notifications: Sequence[Notification]
) -> list[NotificationResponse]:
    """Attach each notification's product, loaded once for the page rather than per row.

    NULL stays NULL: a notification about no single product is valid by design ([D47]).
    """
    products = await product_repo.products_by_id(
        session, {row.product_id for row in notifications if row.product_id is not None}
    )
    return [
        NotificationResponse.model_validate(row).model_copy(
            update={
                "product": (
                    ProductRef.model_validate(products[row.product_id])
                    if row.product_id is not None and row.product_id in products
                    else None
                )
            }
        )
        for row in notifications
    ]


__all__ = ["router"]
