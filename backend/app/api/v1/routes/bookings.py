"""Booked demos, per product (§12, §17, Phase 26).

Scoped to one product. A salesperson looking at SustainX sees SustainX's demos, booked on
SustainX's calendar — not the organisation's. That every internal user may open every
product ([D46]) is a reason to make switching easy, not a reason to merge the lists.

Read-only. A booking exists only through the availability re-check and Calendar
confirmation (§12), so nothing here creates one.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query

from app.api.deps import DbSession
from app.api.schemas import BookingPage, BookingResponse
from app.api.scope import PRODUCT_PREFIX, ProductScope, SegmentFilter, owned_by
from app.domain.enums import BookingStatus
from app.persistence.repositories import bookings as booking_repo

router = APIRouter(prefix=f"{PRODUCT_PREFIX}/bookings", tags=["bookings"])

MAX_PAGE_SIZE = 200


@router.get("", response_model=BookingPage)
async def list_bookings(
    product: ProductScope,
    segment: SegmentFilter,
    session: DbSession,
    booking_status: BookingStatus | None = Query(default=None, alias="status"),
    lead_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
) -> BookingPage:
    """This product's bookings, soonest-starting last.

    `lead_id` narrows to one lead, for the lead page. A lead from another product
    matches nothing rather than erroring, since the product filter is applied first.
    """
    total, bookings = await booking_repo.list_bookings(
        session,
        product_id=product.id,
        segment_id=segment.id if segment is not None else None,
        status=booking_status,
        lead_id=lead_id,
        limit=limit,
        offset=offset,
    )
    return BookingPage(
        total=total,
        limit=limit,
        offset=offset,
        items=[BookingResponse.model_validate(row) for row in bookings],
    )


@router.get("/{booking_id}", response_model=BookingResponse)
async def get_booking(
    booking_id: uuid.UUID, product: ProductScope, session: DbSession
) -> BookingResponse:
    booking = owned_by(
        await booking_repo.get_booking(session, booking_id), product, not_found="Booking not found"
    )
    return BookingResponse.model_validate(booking)


__all__ = ["router"]
