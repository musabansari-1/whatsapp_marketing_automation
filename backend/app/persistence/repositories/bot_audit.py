"""Conversation activity for the bot audit ([D67]).

Metadata only: counts and timestamps of inbound messages per conversation, plus the
lead's phone and product for the flag. Message bodies are never read — a bot's
words look human; its timing does not.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import MessageDirection
from app.persistence.models import Conversation, Lead, Message, WhatsAppSuppression


@dataclass(frozen=True, slots=True)
class ConversationActivity:
    """What the audit scores for one conversation."""

    conversation_id: uuid.UUID
    lead_id: uuid.UUID
    product_id: uuid.UUID
    phone_e164: str
    #: Ordered inbound timestamps inside the window, for the median gap.
    window_starts: tuple[datetime, ...]
    #: All-time inbound total, for the marathon leg.
    lifetime_messages: int


async def recent_activity(session: AsyncSession, *, since: datetime) -> list[ConversationActivity]:
    """Conversations with inbound traffic since `since`, minus suppressed senders.

    A suppressed number is already handled (blocked or opted out) — flagging it
    again would nag about a case someone already decided. Three small queries
    rather than one clever one; an hourly audit over these table sizes does not
    need clever, and each step stays readable.
    """
    candidates = (
        select(Conversation.id, Conversation.lead_id, Conversation.product_id, Lead.phone_e164)
        .join(Lead, Lead.id == Conversation.lead_id)
        .where(
            select(Message.id)
            .where(
                Message.conversation_id == Conversation.id,
                Message.direction == MessageDirection.INBOUND,
                Message.created_at >= since,
            )
            .correlate(Conversation)
            .exists(),
            ~select(WhatsAppSuppression.id)
            .where(WhatsAppSuppression.phone_e164 == Lead.phone_e164)
            .correlate(Lead)
            .exists(),
        )
    )
    rows = (await session.execute(candidates)).all()
    if not rows:
        return []

    conversation_ids = [row[0] for row in rows]

    stamps = await session.execute(
        select(Message.conversation_id, Message.created_at)
        .where(
            Message.direction == MessageDirection.INBOUND,
            Message.created_at >= since,
            Message.conversation_id.in_(conversation_ids),
        )
        .order_by(Message.conversation_id, Message.created_at)
    )
    by_conversation: dict[uuid.UUID, list[datetime]] = defaultdict(list)
    for conversation_id, created_at in stamps.tuples().all():
        if conversation_id is not None:
            by_conversation[conversation_id].append(created_at)

    totals = await session.execute(
        select(Message.conversation_id, func.count())
        .where(
            Message.direction == MessageDirection.INBOUND,
            Message.conversation_id.in_(conversation_ids),
        )
        .group_by(Message.conversation_id)
    )
    lifetime = {conversation_id: int(n) for conversation_id, n in totals.tuples().all()}

    return [
        ConversationActivity(
            conversation_id=conversation_id,
            lead_id=lead_id,
            product_id=product_id,
            phone_e164=phone,
            window_starts=tuple(by_conversation.get(conversation_id, ())),
            lifetime_messages=lifetime.get(conversation_id, 0),
        )
        for conversation_id, lead_id, product_id, phone in rows
    ]


__all__ = ["ConversationActivity", "recent_activity"]
