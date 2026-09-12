"""What "in this product" and "in this segment" mean, per table (§28, Phase 26).

One definition, used by every product-scoped list and by analytics alike. Two copies of
the same filter would drift, and the dashboard would show a list and a count that do not
agree.

**Product is the isolation boundary; segment is a filter inside it.** Every function
takes `product_id` without a default. `None` is accepted, but only as a deliberate
request for every product — combined analytics — never because a caller forgot. A
segment without a product is refused outright: segment slugs are unique only within a
product, so the question has no meaning.

Segment semantics, and why each is what it is:

    audiences, campaigns,     their own `segment_id`
    conversations
    leads                     a member of an audience targeting the segment. A lead has
                              no segment column — it may belong to several ([D48]) — so
                              membership through `audience_contacts` ([D55]) is the only
                              honest answer
    messages                  sent by a campaign targeting the segment, or inside a
                              conversation snapshotted to it
    bookings                  made in a conversation snapshotted to the segment
    notifications             product only; a notification has no segment
"""

from __future__ import annotations

import uuid

from sqlalchemy import or_, select
from sqlalchemy.sql.elements import ColumnElement

from app.persistence.models import (
    Audience,
    AudienceContact,
    Booking,
    Campaign,
    Conversation,
    Lead,
    Message,
    Notification,
)

Conditions = list[ColumnElement[bool]]


def _check(product_id: uuid.UUID | None, segment_id: uuid.UUID | None) -> None:
    if segment_id is not None and product_id is None:
        raise ValueError("A segment filter is only meaningful within a product")


def audience_scope(
    *, product_id: uuid.UUID | None, segment_id: uuid.UUID | None = None
) -> Conditions:
    _check(product_id, segment_id)
    conditions: Conditions = []
    if product_id is not None:
        conditions.append(Audience.product_id == product_id)
    if segment_id is not None:
        conditions.append(Audience.segment_id == segment_id)
    return conditions


def campaign_scope(
    *, product_id: uuid.UUID | None, segment_id: uuid.UUID | None = None
) -> Conditions:
    _check(product_id, segment_id)
    conditions: Conditions = []
    if product_id is not None:
        conditions.append(Campaign.product_id == product_id)
    if segment_id is not None:
        conditions.append(Campaign.segment_id == segment_id)
    return conditions


def conversation_scope(
    *, product_id: uuid.UUID | None, segment_id: uuid.UUID | None = None
) -> Conditions:
    _check(product_id, segment_id)
    conditions: Conditions = []
    if product_id is not None:
        conditions.append(Conversation.product_id == product_id)
    if segment_id is not None:
        conditions.append(Conversation.segment_id == segment_id)
    return conditions


def lead_scope(*, product_id: uuid.UUID | None, segment_id: uuid.UUID | None = None) -> Conditions:
    _check(product_id, segment_id)
    conditions: Conditions = []
    if product_id is not None:
        conditions.append(Lead.product_id == product_id)
    if segment_id is not None:
        conditions.append(
            Lead.id.in_(
                select(AudienceContact.lead_id)
                .join(Audience, Audience.id == AudienceContact.audience_id)
                .where(Audience.segment_id == segment_id, AudienceContact.lead_id.is_not(None))
            )
        )
    return conditions


def message_scope(
    *, product_id: uuid.UUID | None, segment_id: uuid.UUID | None = None
) -> Conditions:
    _check(product_id, segment_id)
    conditions: Conditions = []
    if product_id is not None:
        conditions.append(Message.product_id == product_id)
    if segment_id is not None:
        conditions.append(
            or_(
                Message.campaign_id.in_(
                    select(Campaign.id).where(Campaign.segment_id == segment_id)
                ),
                Message.conversation_id.in_(
                    select(Conversation.id).where(Conversation.segment_id == segment_id)
                ),
            )
        )
    return conditions


def booking_scope(
    *, product_id: uuid.UUID | None, segment_id: uuid.UUID | None = None
) -> Conditions:
    _check(product_id, segment_id)
    conditions: Conditions = []
    if product_id is not None:
        conditions.append(Booking.product_id == product_id)
    if segment_id is not None:
        conditions.append(
            Booking.conversation_id.in_(
                select(Conversation.id).where(Conversation.segment_id == segment_id)
            )
        )
    return conditions


def notification_scope(*, product_id: uuid.UUID | None) -> Conditions:
    return [] if product_id is None else [Notification.product_id == product_id]


__all__ = [
    "Conditions",
    "audience_scope",
    "booking_scope",
    "campaign_scope",
    "conversation_scope",
    "lead_scope",
    "message_scope",
    "notification_scope",
]
