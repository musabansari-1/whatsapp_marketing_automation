"""Conversation, history and lead persistence (specification §9, §10, §16).

Two message tables are in play and the difference matters:

- `messages` is transport state — what was sent to Meta and what Meta said about it.
- `conversation_messages` is the *transcript*, the role/content view the AI reads.

A broadcast produces a `messages` row with no transcript turn (nobody has spoken yet),
and a system note can be a transcript turn with no transport record. Keeping them
separate is what lets each stay accurate.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.conversation import (
    assert_conversation_transition,
    assert_lead_transition,
)
from app.domain.enums import ConversationStatus, LeadStatus, MessageDirection
from app.persistence.models import (
    Campaign,
    CampaignRecipient,
    Conversation,
    ConversationMessage,
    Lead,
    Message,
)

# Transcript roles, matching the LLM transport vocabulary.
ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
ROLE_SYSTEM = "system"


async def list_lead_campaigns(session: AsyncSession, lead_id: uuid.UUID):
    """Campaign snapshots that included this lead, newest campaign first."""
    result = await session.execute(
        select(Campaign, CampaignRecipient)
        .join(CampaignRecipient, CampaignRecipient.campaign_id == Campaign.id)
        .where(CampaignRecipient.lead_id == lead_id)
        .order_by(Campaign.created_at.desc())
    )
    return result.all()


# ---------------------------------------------------------------- conversations


async def get_conversation(
    session: AsyncSession, conversation_id: uuid.UUID
) -> Conversation | None:
    return await session.get(Conversation, conversation_id)


async def get_open_conversation_for_lead(
    session: AsyncSession, lead_id: uuid.UUID
) -> Conversation | None:
    """The lead's current conversation, if one is open.

    Excludes CLOSED: [D36] makes a closed conversation final, so "the lead's
    conversation" means the open one or none at all.
    """
    result = await session.execute(
        select(Conversation)
        .where(
            Conversation.lead_id == lead_id,
            Conversation.status != ConversationStatus.CLOSED,
        )
        .order_by(Conversation.created_at.desc())
        .limit(1)
    )
    return result.scalars().first()


async def get_latest_conversation_for_lead(
    session: AsyncSession, lead_id: uuid.UUID
) -> Conversation | None:
    """The lead's most recent conversation, open or closed.

    The dashboard needs this rather than `get_open_conversation_for_lead`: a lead
    whose conversation ended still has a history worth reading.
    """
    result = await session.execute(
        select(Conversation)
        .where(Conversation.lead_id == lead_id)
        .order_by(Conversation.created_at.desc())
        .limit(1)
    )
    return result.scalars().first()


async def list_conversations(
    session: AsyncSession,
    *,
    product_id: uuid.UUID,
    segment_id: uuid.UUID | None = None,
    status: ConversationStatus | None = None,
    campaign_id: uuid.UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[int, Sequence[Conversation]]:
    """A page of one product's conversations, newest activity first, with the total."""
    from app.persistence.repositories.scoping import conversation_scope

    conditions = conversation_scope(product_id=product_id, segment_id=segment_id)
    if status is not None:
        conditions.append(Conversation.status == status)
    if campaign_id is not None:
        conditions.append(
            Conversation.lead_id.in_(
                select(CampaignRecipient.lead_id)
                .join(Campaign, Campaign.id == CampaignRecipient.campaign_id)
                .where(
                    CampaignRecipient.campaign_id == campaign_id,
                    Campaign.product_id == product_id,
                )
            )
        )

    total = await session.scalar(select(func.count()).select_from(Conversation).where(*conditions))

    result = await session.execute(
        select(Conversation)
        .where(*conditions)
        # Falls back to created_at so a conversation with no traffic yet still sorts
        # sensibly rather than sinking below every older one.
        .order_by(
            func.coalesce(
                Conversation.last_inbound_at,
                Conversation.last_outbound_at,
                Conversation.created_at,
            ).desc()
        )
        .limit(limit)
        .offset(offset)
    )
    return int(total or 0), result.scalars().all()


async def summarize_conversations(
    session: AsyncSession, conversation_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, dict[str, Any]]:
    """Who each conversation is with and its latest message, for the inbox list.

    Two queries for the whole page, not one per row: the leads, and the newest message
    of each conversation (`DISTINCT ON`, so each conversation's row is its latest).
    """
    if not conversation_ids:
        return {}

    leads = await session.execute(
        select(Conversation.id, Lead.full_name, Lead.phone_e164, Lead.opted_out)
        .join(Lead, Lead.id == Conversation.lead_id)
        .where(Conversation.id.in_(conversation_ids))
    )
    summaries: dict[uuid.UUID, dict[str, Any]] = {
        conversation_id: {"lead_name": name, "lead_phone": phone, "opted_out": opted_out}
        for conversation_id, name, phone, opted_out in leads.all()
    }

    latest = await session.execute(
        select(Message.conversation_id, Message.body, Message.direction, Message.created_at)
        .where(Message.conversation_id.in_(conversation_ids))
        .distinct(Message.conversation_id)
        .order_by(Message.conversation_id, Message.created_at.desc())
    )
    for conversation_id, body, direction, created_at in latest.all():
        if conversation_id is None or conversation_id not in summaries:
            continue
        summaries[conversation_id].update(
            last_message=body,
            last_message_direction=direction,
            last_message_at=created_at,
        )
    return summaries


async def transition_conversation(
    session: AsyncSession, conversation: Conversation, target: ConversationStatus
) -> None:
    """Move a conversation to a new state, or refuse (§10).

    The check lives in the domain table, not here, so the AI cannot reach a state the
    specification does not allow just because a handler forgot to look.
    """
    assert_conversation_transition(conversation.status, target)
    conversation.status = target
    if target is ConversationStatus.CLOSED:
        conversation.closed_at = datetime.now(UTC)
    await session.flush()


async def touch_outbound(
    session: AsyncSession, conversation: Conversation, *, at: datetime | None = None
) -> None:
    conversation.last_outbound_at = at or datetime.now(UTC)
    await session.flush()


# ---------------------------------------------------------------- transcript


async def get_history(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    *,
    limit: int | None = None,
) -> list[ConversationMessage]:
    """The transcript in chronological order.

    Ordered by `sequence`, not `created_at`: the latter defaults to `now()`, which is
    the transaction timestamp, so turns written together would tie and their order —
    the order the AI reads the conversation in — would be arbitrary.

    `limit` keeps the *most recent* turns, not the first: an LLM context window is
    finite and the recent turns are what the next reply depends on. The result is
    still returned oldest-first, because that is the order a transcript is read in.
    """
    query = select(ConversationMessage).where(
        ConversationMessage.conversation_id == conversation_id
    )

    if limit is None:
        result = await session.execute(query.order_by(ConversationMessage.sequence))
        return list(result.scalars().all())

    result = await session.execute(query.order_by(ConversationMessage.sequence.desc()).limit(limit))
    return list(reversed(list(result.scalars().all())))


async def count_history(session: AsyncSession, conversation_id: uuid.UUID) -> int:
    total = await session.scalar(
        select(func.count())
        .select_from(ConversationMessage)
        .where(ConversationMessage.conversation_id == conversation_id)
    )
    return int(total or 0)


async def find_turn_for_message(
    session: AsyncSession, *, conversation_id: uuid.UUID, message_id: uuid.UUID
) -> ConversationMessage | None:
    """The transcript turn recorded for a transport message, if any.

    This is what makes turn recording idempotent: a redelivered task finds the turn it
    already wrote instead of appending a second copy of the same customer message.
    """
    result = await session.execute(
        select(ConversationMessage).where(
            ConversationMessage.conversation_id == conversation_id,
            ConversationMessage.message_id == message_id,
        )
    )
    return result.scalars().first()


async def record_turn(
    session: AsyncSession,
    *,
    conversation: Conversation,
    role: str,
    content: str,
    message_id: uuid.UUID | None = None,
    tool_calls: dict[str, Any] | None = None,
) -> ConversationMessage:
    """Append one turn to the transcript."""
    turn = ConversationMessage(
        conversation_id=conversation.id,
        role=role,
        content=content,
        message_id=message_id,
        tool_calls=tool_calls,
    )
    session.add(turn)
    await session.flush()
    return turn


async def record_turn_once(
    session: AsyncSession,
    *,
    conversation: Conversation,
    role: str,
    content: str,
    message_id: uuid.UUID,
    tool_calls: dict[str, Any] | None = None,
) -> tuple[ConversationMessage, bool]:
    """Append a turn unless one already exists for this transport message.

    Returns the turn and whether it was newly created, so a caller can skip the side
    effects of a turn it has already handled (§20).
    """
    existing = await find_turn_for_message(
        session, conversation_id=conversation.id, message_id=message_id
    )
    if existing is not None:
        return existing, False

    turn = await record_turn(
        session,
        conversation=conversation,
        role=role,
        content=content,
        message_id=message_id,
        tool_calls=tool_calls,
    )
    return turn, True


async def get_message(session: AsyncSession, message_id: uuid.UUID) -> Message | None:
    return await session.get(Message, message_id)


async def find_reply_by_idempotency_key(
    session: AsyncSession, idempotency_key: str
) -> Message | None:
    """An outbound reply already recorded under this key.

    The layered defence against double-replying: the deterministic task name collapses
    most redeliveries, and this catches the case where a worker sent the reply and
    crashed before recording it.
    """
    result = await session.execute(
        select(Message).where(Message.idempotency_key == idempotency_key)
    )
    return result.scalars().first()


async def record_outbound_reply(
    session: AsyncSession,
    *,
    lead: Lead,
    conversation: Conversation,
    body: str,
    idempotency_key: str,
    provider_message_id: str | None,
    status: Any,
    sent_at: datetime | None = None,
    template_name: str | None = None,
) -> Message:
    """Record a free-form reply in the transport table.

    `template_name` names the approved template when the outbound is one — the
    first-reply welcome — so the row says what was sent rather than only its
    preview text. Free-form replies leave it None.
    """
    message = Message(
        lead_id=lead.id,
        conversation_id=conversation.id,
        # From the conversation, which is the authority for interaction context
        # (§28.5). Never inferred from the message or the sender's number.
        product_id=conversation.product_id,
        direction=MessageDirection.OUTBOUND,
        status=status,
        provider_message_id=provider_message_id,
        phone_e164=lead.phone_e164,
        template_name=template_name,
        body=body,
        idempotency_key=idempotency_key,
        sent_at=sent_at,
    )
    session.add(message)
    await session.flush()
    return message


# ---------------------------------------------------------------- leads


async def get_lead(session: AsyncSession, lead_id: uuid.UUID) -> Lead | None:
    return await session.get(Lead, lead_id)


async def list_leads(
    session: AsyncSession,
    *,
    product_id: uuid.UUID,
    segment_id: uuid.UUID | None = None,
    status: LeadStatus | None = None,
    campaign_id: uuid.UUID | None = None,
    include_archived: bool = False,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[int, Sequence[Lead]]:
    """A page of one product's leads, newest first, with the total.

    `search` matches name, phone, email or company. Phone is included because it is
    the identity a salesperson actually has in front of them ([D11]) — and searching a
    phone finds only this product's lead for it, never the same person's lead in another
    product ([D41]).
    """
    from app.persistence.repositories.scoping import lead_scope

    conditions = lead_scope(product_id=product_id, segment_id=segment_id)
    if not include_archived:
        conditions.append(Lead.archived_at.is_(None))
    if status is not None:
        conditions.append(Lead.status == status)
    if campaign_id is not None:
        conditions.append(
            Lead.id.in_(
                select(CampaignRecipient.lead_id)
                .join(Campaign, Campaign.id == CampaignRecipient.campaign_id)
                .where(
                    CampaignRecipient.campaign_id == campaign_id,
                    Campaign.product_id == product_id,
                )
            )
        )
    if search:
        pattern = f"%{search.strip()}%"
        conditions.append(
            or_(
                Lead.full_name.ilike(pattern),
                Lead.phone_e164.ilike(pattern),
                Lead.email.ilike(pattern),
                Lead.company.ilike(pattern),
            )
        )

    total = await session.scalar(select(func.count()).select_from(Lead).where(*conditions))

    result = await session.execute(
        select(Lead).where(*conditions).order_by(Lead.created_at.desc()).limit(limit).offset(offset)
    )
    return int(total or 0), result.scalars().all()


async def transition_lead(session: AsyncSession, lead: Lead, target: LeadStatus) -> None:
    """Move a lead to a new state, or refuse (§10, [D18]).

    Every status change goes through here. [D18] makes the backend solely responsible
    for `lead.status`, and a single validated path is what makes that enforceable
    rather than a convention.
    """
    assert_lead_transition(lead.status, target)
    lead.status = target
    if target is LeadStatus.OPTED_OUT:
        lead.opted_out = True
    await session.flush()


async def update_lead_details(
    session: AsyncSession,
    lead: Lead,
    *,
    full_name: str | None = None,
    email: str | None = None,
    company: str | None = None,
    designation: str | None = None,
    city: str | None = None,
) -> Lead:
    """Update the human-editable fields.

    Deliberately cannot touch `status`, `qualification_data` or `opted_out`: the first
    belongs to `transition_lead`, the second is the AI's extraction target ([D18]), and
    the third is consent, which is not an editable attribute.
    """
    if full_name is not None:
        lead.full_name = full_name
    if email is not None:
        lead.email = email
    if company is not None:
        lead.company = company
    if designation is not None:
        lead.designation = designation
    if city is not None:
        lead.city = city

    await session.flush()
    return lead


async def merge_qualification_data(
    session: AsyncSession, lead: Lead, extracted: dict[str, Any]
) -> Lead:
    """Merge AI-extracted facts into `leads.qualification_data` ([D18]).

    Merged rather than replaced so a later turn that mentions only one fact does not
    erase everything learned earlier. Note what this function does *not* do: it never
    touches `lead.status`. The AI extracts; the backend decides.
    """
    merged = dict(lead.qualification_data or {})
    merged.update(extracted)
    lead.qualification_data = merged
    await session.flush()
    return lead


__all__ = [
    "ROLE_ASSISTANT",
    "ROLE_SYSTEM",
    "ROLE_USER",
    "count_history",
    "find_reply_by_idempotency_key",
    "find_turn_for_message",
    "get_conversation",
    "get_history",
    "get_latest_conversation_for_lead",
    "get_lead",
    "get_message",
    "get_open_conversation_for_lead",
    "list_conversations",
    "list_leads",
    "merge_qualification_data",
    "record_outbound_reply",
    "record_turn",
    "record_turn_once",
    "touch_outbound",
    "transition_conversation",
    "transition_lead",
    "update_lead_details",
]
