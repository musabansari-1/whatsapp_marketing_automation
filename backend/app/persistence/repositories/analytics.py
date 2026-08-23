"""Overview metrics (specification §17).

The nine numbers §17 names, computed from the tables that own them rather than from a
running tally. Counters can drift — a crashed worker, a manual correction, a migration
— and a dashboard that quietly disagrees with the data is worse than a slow one.

Each metric is defined here rather than in the API layer, because "replies" and
"interested" are judgements about which table answers the question, and those
definitions belong somewhere reviewable.
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
from app.persistence.models import Booking, Campaign, Lead, Message

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


async def overview(session: AsyncSession, *, user_id: uuid.UUID | None = None) -> OverviewMetrics:
    """Compute the overview in one pass.

    Delivery counts come from `messages` rather than the campaign counters: a message
    sent as a conversation reply belongs in "sent" too, and it has no campaign.
    """
    campaigns = await _count(session, select(func.count()).select_from(Campaign))
    campaigns_sending = await _count(
        session,
        select(func.count())
        .select_from(Campaign)
        .where(Campaign.status == CampaignStatus.PROCESSING),
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
            outbound,
            Message.status.in_([MessageStatus.SENT, MessageStatus.DELIVERED, MessageStatus.READ]),
        ),
    )
    delivered = await _count(
        session,
        select(func.count())
        .select_from(Message)
        .where(
            outbound,
            Message.status.in_([MessageStatus.DELIVERED, MessageStatus.READ]),
        ),
    )
    read = await _count(
        session,
        select(func.count())
        .select_from(Message)
        .where(outbound, Message.status == MessageStatus.READ),
    )
    failed = await _count(
        session,
        select(func.count())
        .select_from(Message)
        .where(outbound, Message.status == MessageStatus.FAILED),
    )

    # Replies are counted as *people*, not messages: a customer who sends five messages
    # is one reply to a salesperson reading this, and counting messages would make
    # engagement look like reach.
    replies = await _count(
        session,
        select(func.count(func.distinct(Message.lead_id))).where(
            Message.direction == MessageDirection.INBOUND, Message.lead_id.is_not(None)
        ),
    )

    total_leads = await _count(session, select(func.count()).select_from(Lead))
    interested = await _count(
        session,
        select(func.count()).select_from(Lead).where(Lead.status.in_(_INTERESTED_STATES)),
    )
    qualified = await _count(
        session,
        select(func.count()).select_from(Lead).where(Lead.status.in_(_QUALIFIED_STATES)),
    )

    # Confirmed only: a cancelled or failed attempt is not a demo anyone will attend.
    demos_booked = await _count(
        session,
        select(func.count()).select_from(Booking).where(Booking.status == BookingStatus.CONFIRMED),
    )

    unread = 0
    if user_id is not None:
        from app.persistence.models import Notification

        unread = await _count(
            session,
            select(func.count())
            .select_from(Notification)
            .where(Notification.user_id == user_id, Notification.is_read.is_(False)),
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


async def leads_by_status(session: AsyncSession) -> dict[str, int]:
    """The funnel, as a count per state.

    Every state appears, including the empty ones: a funnel with gaps hides where
    leads are actually stalling.
    """
    result = await session.execute(select(Lead.status, func.count()).group_by(Lead.status))
    counts = {row[0].value: int(row[1]) for row in result.all()}
    return {status.value: counts.get(status.value, 0) for status in LeadStatus}


async def _count(session: AsyncSession, statement: Select[tuple[int]]) -> int:
    return int(await session.scalar(statement) or 0)


__all__ = ["OverviewMetrics", "leads_by_status", "overview"]
