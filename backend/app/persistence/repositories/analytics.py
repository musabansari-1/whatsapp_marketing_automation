"""Overview metrics (specification §17, §28, [D53]).

The nine numbers §17 names, computed from the tables that own them rather than from a
running tally. Counters can drift — a crashed worker, a manual correction, a migration
— and a dashboard that quietly disagrees with the data is worse than a slow one.

Each metric is defined here rather than in the API layer, because "replies" and
"interested" are judgements about which table answers the question, and those
definitions belong somewhere reviewable.

**Every count has deliberate product semantics.** One implementation serves the three
views [D53] requires, differing only in the scope it is given:

    one product                  AnalyticsScope.for_product(product_id)
    one segment of a product     AnalyticsScope.for_product(product_id, segment_id=…)
    every product, combined      AnalyticsScope.all_products()

Combined is always an explicit request, never what a missing product falls back to.
What "in a segment" means per table lives once, in `scoping.py`. The case to know about is
leads: they have no segment of their own and count through audience membership, so one
lead can count in several segments, and segment figures are not meant to sum to their
product's.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from app.domain.enums import (
    BookingStatus,
    CampaignStatus,
    LeadStatus,
    MessageDirection,
    MessageStatus,
)
from app.persistence.models import Booking, Campaign, Lead, Message, Notification
from app.persistence.repositories.scoping import (
    booking_scope,
    campaign_scope,
    lead_scope,
    message_scope,
    notification_scope,
)

# Lead states that mean the customer showed interest, whether or not they went further.
# QUALIFIED and DEMO_BOOKED are included because a qualified lead is by definition an
# interested one, and a dashboard where "interested" drops as leads progress would read
# as leads being lost.
_INTERESTED_STATES = (
    LeadStatus.INTERESTED,
    LeadStatus.QUALIFYING,
    LeadStatus.QUALIFIED,
    LeadStatus.DEMO_OFFERED,
    LeadStatus.DEMO_BOOKED,
)

# Likewise: a lead with a demo booked has been qualified, whatever its current state.
_QUALIFIED_STATES = (LeadStatus.QUALIFIED, LeadStatus.DEMO_OFFERED, LeadStatus.DEMO_BOOKED)


@dataclass(frozen=True, slots=True)
class AnalyticsScope:
    """Which rows a metric counts.

    Constructed through the two class methods, so a call site says in words which view
    it wants. A segment without a product is refused: segment slugs are unique only
    within a product, so the question would have no meaning.
    """

    product_id: uuid.UUID | None
    segment_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        if self.segment_id is not None and self.product_id is None:
            raise ValueError("A segment scope requires a product")

    @classmethod
    def for_product(
        cls, product_id: uuid.UUID, *, segment_id: uuid.UUID | None = None
    ) -> AnalyticsScope:
        return cls(product_id=product_id, segment_id=segment_id)

    @classmethod
    def all_products(cls) -> AnalyticsScope:
        return cls(product_id=None)


@dataclass(frozen=True, slots=True)
class OverviewMetrics:
    """The §17 overview, plus the totals a salesperson needs for context."""

    campaigns: int
    campaigns_sending: int
    messages_sent: int
    delivered: int
    read: int
    failed: int
    replies: int
    total_leads: int
    interested: int
    qualified: int
    demos_booked: int
    unread_notifications: int = 0


async def overview(
    session: AsyncSession, *, scope: AnalyticsScope, user_id: uuid.UUID | None = None
) -> OverviewMetrics:
    """Compute the overview for a scope.

    Delivery counts come from `messages` rather than the campaign counters: a message
    sent as a conversation reply belongs in "sent" too, and it has no campaign.
    """
    in_campaigns = campaign_scope(product_id=scope.product_id, segment_id=scope.segment_id)
    in_messages = message_scope(product_id=scope.product_id, segment_id=scope.segment_id)
    in_leads = lead_scope(product_id=scope.product_id, segment_id=scope.segment_id)
    in_bookings = booking_scope(product_id=scope.product_id, segment_id=scope.segment_id)

    campaigns = await _count(
        session, select(func.count()).select_from(Campaign).where(*in_campaigns)
    )
    campaigns_sending = await _count(
        session,
        select(func.count())
        .select_from(Campaign)
        .where(*in_campaigns, Campaign.status == CampaignStatus.PROCESSING),
    )

    outbound = Message.direction == MessageDirection.OUTBOUND

    # SENT counts everything that left the platform, so the later states are included:
    # a delivered message was sent, and reporting otherwise would show "sent" falling
    # as delivery receipts arrive.
    messages_sent = await _count(
        session,
        select(func.count())
        .select_from(Message)
        .where(
            *in_messages,
            outbound,
            Message.status.in_([MessageStatus.SENT, MessageStatus.DELIVERED, MessageStatus.READ]),
        ),
    )
    delivered = await _count(
        session,
        select(func.count())
        .select_from(Message)
        .where(
            *in_messages,
            outbound,
            Message.status.in_([MessageStatus.DELIVERED, MessageStatus.READ]),
        ),
    )
    read = await _count(
        session,
        select(func.count())
        .select_from(Message)
        .where(*in_messages, outbound, Message.status == MessageStatus.READ),
    )
    failed = await _count(
        session,
        select(func.count())
        .select_from(Message)
        .where(*in_messages, outbound, Message.status == MessageStatus.FAILED),
    )

    # Replies are counted as *people*, not messages: a customer who sends five messages
    # is one reply to a salesperson reading this, and counting messages would make
    # engagement look like reach. Combined, a person who replied in two products counts
    # twice, because they are two leads ([D41]).
    replies = await _count(
        session,
        select(func.count(func.distinct(Message.lead_id))).where(
            *in_messages,
            Message.direction == MessageDirection.INBOUND,
            Message.lead_id.is_not(None),
        ),
    )

    total_leads = await _count(session, select(func.count()).select_from(Lead).where(*in_leads))
    interested = await _count(
        session,
        select(func.count())
        .select_from(Lead)
        .where(*in_leads, Lead.status.in_(_INTERESTED_STATES)),
    )
    qualified = await _count(
        session,
        select(func.count()).select_from(Lead).where(*in_leads, Lead.status.in_(_QUALIFIED_STATES)),
    )

    # Confirmed only: a cancelled or failed attempt is not a demo anyone will attend.
    demos_booked = await _count(
        session,
        select(func.count())
        .select_from(Booking)
        .where(*in_bookings, Booking.status == BookingStatus.CONFIRMED),
    )

    unread = 0
    if user_id is not None:
        # Product only, never segment: a notification carries a product and no segment.
        unread = await _count(
            session,
            select(func.count())
            .select_from(Notification)
            .where(
                *notification_scope(product_id=scope.product_id),
                Notification.user_id == user_id,
                Notification.is_read.is_(False),
            ),
        )

    return OverviewMetrics(
        campaigns=campaigns,
        campaigns_sending=campaigns_sending,
        messages_sent=messages_sent,
        delivered=delivered,
        read=read,
        failed=failed,
        replies=replies,
        total_leads=total_leads,
        interested=interested,
        qualified=qualified,
        demos_booked=demos_booked,
        unread_notifications=unread,
    )


async def leads_by_status(session: AsyncSession, *, scope: AnalyticsScope) -> dict[str, int]:
    """The funnel, as a count per state.

    Every state appears, including the empty ones: a funnel with gaps hides where
    leads are actually stalling.
    """
    result = await session.execute(
        select(Lead.status, func.count())
        .where(*lead_scope(product_id=scope.product_id, segment_id=scope.segment_id))
        .group_by(Lead.status)
    )
    counts = {row[0].value: int(row[1]) for row in result.all()}
    return {status.value: counts.get(status.value, 0) for status in LeadStatus}


async def _count(session: AsyncSession, statement: Select[tuple[int]]) -> int:
    return int(await session.scalar(statement) or 0)


__all__ = ["AnalyticsScope", "OverviewMetrics", "leads_by_status", "overview"]
