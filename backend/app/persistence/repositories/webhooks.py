"""Webhook event persistence and inbound-message state (§8, §20, [D5], [D16])."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.conversation import can_transition_conversation, can_transition_lead
from app.domain.enums import (
    ConversationStatus,
    LeadStatus,
    MessageDirection,
    MessageStatus,
    RecipientStatus,
    SuppressionReason,
    WebhookEventStatus,
)
from app.persistence.models import (
    Campaign,
    CampaignRecipient,
    Conversation,
    ConversationMessage,
    Lead,
    Message,
    WebhookEvent,
    WhatsAppSuppression,
)
from app.persistence.repositories import suppressions as suppression_repo

# Meta status -> the recipient status it implies. Only forward progress is applied;
# see `apply_status_to_recipient`.
_RECIPIENT_STATUS = {
    MessageStatus.SENT: RecipientStatus.SENT,
    MessageStatus.DELIVERED: RecipientStatus.DELIVERED,
    MessageStatus.READ: RecipientStatus.READ,
    MessageStatus.FAILED: RecipientStatus.FAILED,
}

# Ordering used to reject out-of-order status regressions.
_PROGRESS = {
    RecipientStatus.PENDING: 0,
    RecipientStatus.QUEUED: 1,
    RecipientStatus.SENT: 2,
    RecipientStatus.DELIVERED: 3,
    RecipientStatus.READ: 4,
}


def derive_event_id(payload: dict[str, Any], raw_body: bytes) -> str:
    """A stable identifier for one webhook delivery (§8 duplicate protection).

    Meta does not supply a delivery ID, so one is derived. Where the payload contains
    message or status IDs those are used, because Meta may redeliver the same events
    with a different envelope. Otherwise the body digest is the fallback.
    """
    ids: list[str] = []
    for entry in payload.get("entry", []) if isinstance(payload.get("entry"), list) else []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes", []) if isinstance(entry.get("changes"), list) else []:
            if not isinstance(change, dict):
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            for key in ("messages", "statuses"):
                for item in value.get(key, []) if isinstance(value.get(key), list) else []:
                    if isinstance(item, dict) and isinstance(item.get("id"), str):
                        # A status and a message can share an ID, so the kind is part
                        # of the identity: "delivered" is a different event from the
                        # message itself.
                        marker = item.get("status", "message") if key == "statuses" else "message"
                        ids.append(f"{item['id']}:{marker}")

    basis = "|".join(sorted(ids)) if ids else raw_body.decode("utf-8", errors="replace")
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


async def record(
    session: AsyncSession, *, provider_event_id: str, payload: dict[str, Any]
) -> WebhookEvent:
    """Persist a delivery. Raises IntegrityError on a duplicate."""
    event = WebhookEvent(
        provider_event_id=provider_event_id,
        event_type="whatsapp",
        payload=payload,
        status=WebhookEventStatus.RECEIVED,
    )
    session.add(event)
    await session.flush()
    return event


async def get_event(session: AsyncSession, event_id: uuid.UUID) -> WebhookEvent | None:
    return await session.get(WebhookEvent, event_id)


async def mark_processed(session: AsyncSession, event: WebhookEvent) -> None:
    event.status = WebhookEventStatus.PROCESSED
    event.processed_at = datetime.now(UTC)
    await session.flush()


async def mark_failed(session: AsyncSession, event: WebhookEvent, reason: str) -> None:
    event.status = WebhookEventStatus.FAILED
    event.error_message = reason[:1000]
    await session.flush()


# ---------------------------------------------------------------- inbound


async def get_or_create_lead(
    session: AsyncSession,
    *,
    product_id: uuid.UUID,
    phone_e164: str,
    full_name: str | None = None,
) -> Lead:
    """Find or create the lead for an inbound number, within its product ([D41]).

    A customer can message the business without ever having been in an audience, so
    inbound traffic must be able to create a lead. The product comes from the number
    they messaged, resolved by the caller before this is reached — there is no longer
    any path that creates a lead without one.

    Identity is `(product, phone)`. The same human messaging two products' numbers is
    two leads, each with its own status, qualification and conversation; within one
    product they remain exactly one lead.
    """
    statement = (
        pg_insert(Lead)
        .values(product_id=product_id, phone_e164=phone_e164, full_name=full_name)
        # The final identity index, so concurrent deliveries for one new number
        # collapse to a single row.
        .on_conflict_do_nothing(
            index_elements=[Lead.product_id, Lead.phone_e164],
            index_where=Lead.product_id.is_not(None),
        )
    )
    await session.execute(statement)
    await session.flush()

    lead = (
        await session.execute(
            select(Lead).where(
                Lead.product_id == product_id, Lead.phone_e164 == phone_e164
            )
        )
    ).scalar_one()

    # Only fill a blank: a WhatsApp profile name must not overwrite the name the
    # client supplied in their audience file.
    if full_name and not lead.full_name:
        lead.full_name = full_name
        await session.flush()

    # A number suppressed earlier must not come back as a contactable lead. Cheap, and
    # it closes the window where an opted-out customer messages again and the row looks
    # fresh until something else notices.
    if await suppression_repo.enforce_mirror_for(session, phone_e164):
        await session.refresh(lead)

    return lead


async def get_or_create_conversation(session: AsyncSession, lead: Lead) -> Conversation:
    """The lead's open conversation, creating one if none is active.

    A CLOSED conversation is not reopened: a customer messaging again after a closed
    thread starts a new one, which keeps each conversation's history coherent for the
    AI in Phase 10.

    A new conversation **snapshots its product and segment** (§28.5, [D48]). An existing
    one is returned untouched: context is frozen at creation and never re-derived, so a
    lead later joining another segment's audience cannot retarget a conversation already
    under way.
    """
    existing = (
        await session.execute(
            select(Conversation)
            .where(
                Conversation.lead_id == lead.id,
                Conversation.status != ConversationStatus.CLOSED,
            )
            .order_by(Conversation.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    if existing is not None:
        # Deliberately not refreshed. §28.5 makes the snapshot immutable.
        return existing

    conversation = Conversation(
        lead_id=lead.id,
        status=ConversationStatus.ACTIVE,
        # The lead is authoritative for product: a conversation belongs to the same
        # product as the lead it is with, and nothing else may disagree. NULL while the
        # transitional inbound path has not attributed the lead — Phase 23 closes that.
        product_id=lead.product_id,
        segment_id=await attributable_segment(session, lead),
    )
    session.add(conversation)
    await session.flush()
    return conversation


async def attributable_segment(session: AsyncSession, lead: Lead) -> uuid.UUID | None:
    """The segment this conversation operates under, or None when nothing proves one.

    A lead may belong to several segments within a product ([D48]), so the segment
    cannot come from the lead. It has to come from the interaction that caused the
    conversation — and an inbound message carries no campaign: `Message.campaign_id` is
    left unset on the inbound path, so the only evidence available is which campaigns
    actually messaged this lead.

    **Exactly one campaign reached them, so exactly one campaign can have prompted the
    reply.** That campaign's segment is the operative one. Recipients that were never
    sent — PENDING, QUEUED, SKIPPED, FAILED — are excluded, because a message that never
    arrived cannot have caused an answer.

    With none, or with several, there is no unambiguous attribution and the segment stays
    NULL. No "most recent wins": nothing in §7 or §8 makes recency evidence, and a
    conversation run under the wrong segment's instructions is worse than one run under
    the product's shared defaults.

    Returns NULL for a product-wide campaign too, which is the same answer arrived at
    honestly: that campaign targeted no segment.
    """
    reached = (
        (
            await session.execute(
                select(Campaign.segment_id)
                .join(CampaignRecipient, CampaignRecipient.campaign_id == Campaign.id)
                .where(
                    CampaignRecipient.lead_id == lead.id,
                    CampaignRecipient.status.in_(
                        (
                            RecipientStatus.SENT,
                            RecipientStatus.DELIVERED,
                            RecipientStatus.READ,
                        )
                    ),
                )
                .distinct()
            )
        )
        .scalars()
        .all()
    )

    if len(reached) != 1:
        return None
    return reached[0]


async def record_inbound_message(
    session: AsyncSession,
    *,
    lead: Lead,
    conversation: Conversation,
    provider_message_id: str,
    body: str | None,
    received_at: datetime,
) -> Message | None:
    """Store an inbound message, or None if it was already stored.

    The unique constraint on provider_message_id makes this idempotent, which matters
    because Meta redelivers and because a task may be retried (§20).

    Updating `last_inbound_at` here is what opens the 24-hour customer service window
    that free-form replies depend on.
    """
    already = (
        await session.execute(
            select(Message).where(Message.provider_message_id == provider_message_id)
        )
    ).scalar_one_or_none()
    if already is not None:
        return None

    message = Message(
        lead_id=lead.id,
        conversation_id=conversation.id,
        # From the conversation the routing already established (§28.5).
        product_id=conversation.product_id,
        direction=MessageDirection.INBOUND,
        status=MessageStatus.DELIVERED,
        provider_message_id=provider_message_id,
        phone_e164=lead.phone_e164,
        body=body,
        delivered_at=received_at,
    )
    session.add(message)
    # Flushed here so the transcript turn can carry the message's ID. That link is
    # what lets the Phase 9 conversation handler recognise a turn it has already
    # recorded instead of appending a second copy of the same customer message.
    await session.flush()

    session.add(
        ConversationMessage(
            conversation_id=conversation.id,
            role="user",
            content=body or "",
            message_id=message.id,
        )
    )

    conversation.last_inbound_at = received_at
    # Their turn is over, so it is ours again. Guarded by the §10 table rather than
    # assigned directly: a conversation in BOOKING stays in BOOKING.
    if can_transition_conversation(conversation.status, ConversationStatus.ACTIVE):
        conversation.status = ConversationStatus.ACTIVE

    # A reply is the first signal a lead is reachable and interested enough to
    # respond. Later states belong to the AI's qualification flow ([D9]).
    if lead.status in (LeadStatus.NEW, LeadStatus.CONTACTED) and can_transition_lead(
        lead.status, LeadStatus.RESPONDED
    ):
        lead.status = LeadStatus.RESPONDED

    await session.flush()
    return message


async def suppress(
    session: AsyncSession,
    *,
    lead: Lead,
    source_message_id: str | None,
    reason: SuppressionReason = SuppressionReason.OPT_OUT_KEYWORD,
) -> bool:
    """Record a global opt-out ([D5], [D16], [D42]).

    Writes the phone-level suppression row — authoritative, global across every product,
    and outliving any lead — then makes **every** lead sharing that number agree with it.

    Propagating to all of them rather than to the one in this conversation is what [D41]
    made necessary: consent belongs to the person, and one human can now be a lead in
    several products. A STOP through Academicae has to suppress the SustainX row too, or
    the dashboard and the CRM would show a contactable lead for someone who has said
    stop.

    The suppression row and the mirrors are written in the caller's transaction, so
    there is no committed state where one exists without the other.

    Returns False when the number was already suppressed, so a repeated STOP does not
    re-notify. The mirror is enforced either way, which is what makes a repeat safe
    rather than merely harmless.
    """
    statement = (
        pg_insert(WhatsAppSuppression)
        .values(
            phone_e164=lead.phone_e164,
            reason=reason,
            source_message_id=source_message_id,
            lead_id=lead.id,
        )
        .on_conflict_do_nothing(index_elements=[WhatsAppSuppression.phone_e164])
    )
    result = await session.execute(statement)
    await session.flush()

    # Every lead for this number, in every product, including rows created before this
    # STOP and rows that have no product yet. `enforce_mirror` owns the documented
    # exception to `transition_lead`.
    await suppression_repo.enforce_mirror_for(session, lead.phone_e164)

    # ON CONFLICT DO NOTHING reports zero rows when the number was already
    # suppressed, which is how a repeated STOP is distinguished from a new one.
    return bool(cast("CursorResult[Any]", result).rowcount)


async def cancel_pending_sends(session: AsyncSession, phone_e164: str) -> int:
    """Skip any not-yet-sent campaign recipients for a number that just opted out.

    Without this, a broadcast already prepared would still message someone who said
    stop moments ago — the failure [D16] exists to prevent.
    """
    result = await session.execute(
        update(CampaignRecipient)
        .where(
            CampaignRecipient.phone_e164 == phone_e164,
            CampaignRecipient.status.in_([RecipientStatus.PENDING, RecipientStatus.QUEUED]),
        )
        .values(status=RecipientStatus.SKIPPED)
    )
    await session.flush()
    return int(cast("CursorResult[Any]", result).rowcount or 0)


# ---------------------------------------------------------------- status updates


async def apply_status(
    session: AsyncSession,
    *,
    provider_message_id: str,
    new_status: MessageStatus,
    occurred_at: datetime,
    error_code: str | None,
    error_message: str | None,
) -> bool:
    """Apply a delivery/read/failure notification (§8).

    Returns False when nothing matched, which is normal: Meta also reports statuses
    for messages sent outside this platform.
    """
    message = (
        await session.execute(
            select(Message).where(Message.provider_message_id == provider_message_id)
        )
    ).scalar_one_or_none()

    matched = False
    if message is not None:
        _apply_to_message(message, new_status, occurred_at, error_code, error_message)
        matched = True

    recipient = (
        await session.execute(
            select(CampaignRecipient).where(
                CampaignRecipient.provider_message_id == provider_message_id
            )
        )
    ).scalar_one_or_none()
    if recipient is not None:
        _apply_to_recipient(recipient, new_status, occurred_at, error_code, error_message)
        matched = True

    if matched:
        await session.flush()
    return matched


def _apply_to_message(
    message: Message,
    new_status: MessageStatus,
    occurred_at: datetime,
    error_code: str | None,
    error_message: str | None,
) -> None:
    if new_status is MessageStatus.FAILED:
        message.status = MessageStatus.FAILED
        message.failed_at = occurred_at
        message.error_code = error_code
        message.error_message = error_message
        return

    match new_status:
        case MessageStatus.SENT:
            message.sent_at = message.sent_at or occurred_at
        case MessageStatus.DELIVERED:
            message.delivered_at = message.delivered_at or occurred_at
        case MessageStatus.READ:
            message.read_at = message.read_at or occurred_at
        case _:
            pass

    # Webhooks arrive out of order often enough to matter; a `delivered` landing after
    # `read` must not walk the status backwards.
    if _rank(new_status) > _rank(message.status):
        message.status = new_status


def _apply_to_recipient(
    recipient: CampaignRecipient,
    new_status: MessageStatus,
    occurred_at: datetime,
    error_code: str | None,
    error_message: str | None,
) -> None:
    target = _RECIPIENT_STATUS.get(new_status)
    if target is None:
        return

    if target is RecipientStatus.FAILED:
        recipient.status = RecipientStatus.FAILED
        recipient.failed_at = occurred_at
        recipient.error_code = error_code
        recipient.error_message = error_message
        return

    match target:
        case RecipientStatus.SENT:
            recipient.sent_at = recipient.sent_at or occurred_at
        case RecipientStatus.DELIVERED:
            recipient.delivered_at = recipient.delivered_at or occurred_at
        case RecipientStatus.READ:
            recipient.read_at = recipient.read_at or occurred_at
        case _:
            pass

    if _PROGRESS.get(target, -1) > _PROGRESS.get(recipient.status, -1):
        recipient.status = target


def _rank(status: MessageStatus) -> int:
    order = {
        MessageStatus.QUEUED: 0,
        MessageStatus.SENT: 1,
        MessageStatus.DELIVERED: 2,
        MessageStatus.READ: 3,
        MessageStatus.FAILED: 4,
    }
    return order.get(status, -1)
