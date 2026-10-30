"""Lead and conversation endpoints, per product (specification §9, §10, §16, §17, §28).

Read-mostly. The dashboard shows what the AI and the customer said and lets a human
correct a lead's details. A team member may also reply into an open conversation: the
endpoint checks the window, suppression and conversation state so the dashboard can say
why at once, then *queues* the reply. The worker sends it and checks again, because it
is where the 24-hour window and suppression checks live, and consent can be withdrawn
between the click and the send.

Every route is inside one product (Phase 26). One person who enquired about two products
is two leads ([D41]), each with its own address under its own product; nothing here
merges them because the phone number matches. A conversation's product and segment were
snapshotted at creation (§28.5) and are only read here — viewing it from the dashboard
never re-derives either.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import AdminUser, CurrentUser, DbSession, Queue, SettingsDep
from app.api.schemas import (
    ConversationDetailResponse,
    ConversationListItem,
    ConversationPage,
    ConversationResponse,
    ConversationTurnResponse,
    LeadPage,
    LeadCampaignResponse,
    LeadResponse,
    TeamReplyAccepted,
    TeamReplyRequest,
    UpdateLeadRequest,
)
from app.api.scope import (
    PRODUCT_PREFIX,
    ActiveProductScope,
    ProductScope,
    SegmentFilter,
    owned_by,
)
from app.core.logging import get_logger
from app.domain.conversation import LeadStateError
from app.domain.enums import ConversationStatus, LeadStatus, MessageDirection
from app.domain.whatsapp_webhook import is_within_service_window
from app.integrations.queue import TaskRequest, TaskType
from app.persistence.models import Conversation, Lead, Product
from app.persistence.repositories import conversations as conversation_repo
from app.persistence.repositories import suppressions as suppression_repo

router = APIRouter(prefix=PRODUCT_PREFIX, tags=["conversations"])
logger = get_logger(__name__)

MAX_PAGE_SIZE = 200


@router.get("/leads", response_model=LeadPage)
async def list_leads(
    product: ProductScope,
    segment: SegmentFilter,
    session: DbSession,
    lead_status: LeadStatus | None = Query(default=None, alias="status"),
    campaign_id: uuid.UUID | None = Query(default=None),
    include_archived: bool = Query(default=False),
    search: str | None = Query(default=None, max_length=255),
    limit: int = Query(default=50, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
) -> LeadPage:
    """This product's leads.

    With a segment, the leads that are members of an audience targeting it ([D48],
    [D55]) — a lead has no segment of its own, and may appear under several.
    """
    total, leads = await conversation_repo.list_leads(
        session,
        product_id=product.id,
        segment_id=segment.id if segment is not None else None,
        status=lead_status,
        campaign_id=campaign_id,
        include_archived=include_archived,
        search=search,
        limit=limit,
        offset=offset,
    )
    return LeadPage(
        total=total,
        limit=limit,
        offset=offset,
        items=[LeadResponse.model_validate(lead) for lead in leads],
    )


@router.post("/leads/{lead_id}/archive", response_model=LeadResponse)
async def archive_lead(lead_id: uuid.UUID, user: AdminUser, product: ProductScope, session: DbSession) -> LeadResponse:
    lead = await _require_lead(session, product, lead_id)
    lead.archived_at = lead.archived_at or datetime.now(UTC)
    await session.flush()
    return LeadResponse.model_validate(lead)


@router.post("/leads/{lead_id}/restore", response_model=LeadResponse)
async def restore_lead(lead_id: uuid.UUID, user: AdminUser, product: ProductScope, session: DbSession) -> LeadResponse:
    lead = await _require_lead(session, product, lead_id)
    lead.archived_at = None
    await session.flush()
    return LeadResponse.model_validate(lead)


@router.get("/leads/{lead_id}", response_model=LeadResponse)
async def get_lead(lead_id: uuid.UUID, product: ProductScope, session: DbSession) -> LeadResponse:
    return LeadResponse.model_validate(await _require_lead(session, product, lead_id))


@router.get("/leads/{lead_id}/campaigns", response_model=list[LeadCampaignResponse])
async def list_lead_campaigns(
    lead_id: uuid.UUID, product: ProductScope, session: DbSession
) -> list[LeadCampaignResponse]:
    """Campaign snapshots that included this lead, with the recorded delivery status."""
    await _require_lead(session, product, lead_id)
    records = await conversation_repo.list_lead_campaigns(session, lead_id)
    return [
        LeadCampaignResponse(
            campaign_id=campaign.id,
            campaign_name=campaign.name,
            campaign_created_at=campaign.created_at,
            recipient_status=recipient.status,
            sent_at=recipient.sent_at,
        )
        for campaign, recipient in records
    ]


@router.patch("/leads/{lead_id}", response_model=LeadResponse)
async def update_lead(
    lead_id: uuid.UUID,
    payload: UpdateLeadRequest,
    user: CurrentUser,
    product: ProductScope,
    session: DbSession,
) -> LeadResponse:
    """Correct a lead's details, and optionally its status.

    Allowed for an inactive product: correcting a record starts no customer activity.

    A status change here goes through the same §10 transition table the worker uses.
    That is deliberate: [D18] says the *backend* owns `lead.status`, not that no human
    may ever correct it — but a human correcting it must not be able to reach a state
    the specification does not allow either.
    """
    lead = await _require_lead(session, product, lead_id)

    await conversation_repo.update_lead_details(
        session,
        lead,
        full_name=payload.full_name,
        email=payload.email,
        company=payload.company,
        designation=payload.designation,
        city=payload.city,
    )

    if payload.status is not None and payload.status is not lead.status:
        try:
            await conversation_repo.transition_lead(session, lead, payload.status)
        except LeadStateError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        logger.info(
            "lead_status_changed_by_user",
            lead_id=str(lead.id),
            new_status=payload.status.value,
            user_id=str(user.id),
        )

    await session.commit()
    return LeadResponse.model_validate(lead)


@router.get("/leads/{lead_id}/conversation", response_model=ConversationDetailResponse)
async def get_lead_conversation(
    lead_id: uuid.UUID,
    product: ProductScope,
    session: DbSession,
    settings: SettingsDep,
    limit: int = Query(default=100, ge=1, le=MAX_PAGE_SIZE),
) -> ConversationDetailResponse:
    """The lead's most recent conversation, open or closed.

    Most recent rather than open: [D36] ends a conversation permanently, and a lead
    whose thread closed still has a history worth reading.
    """
    lead = await _require_lead(session, product, lead_id)
    conversation = await conversation_repo.get_latest_conversation_for_lead(session, lead.id)
    if conversation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="This lead has no conversation yet",
        )

    return await _detail(session, settings, conversation, lead, limit)


@router.post(
    "/leads/{lead_id}/conversation/reply",
    response_model=TeamReplyAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def reply_to_lead(
    lead_id: uuid.UUID,
    payload: TeamReplyRequest,
    user: CurrentUser,
    product: ActiveProductScope,
    session: DbSession,
    settings: SettingsDep,
    queue: Queue,
) -> TeamReplyAccepted:
    """Queue a team member's reply into the lead's open conversation.

    The assistant is paused for `pause_assistant_hours` before the reply is queued, so
    it cannot answer the customer's next message over the person. 409 with the reason
    when the reply cannot be sent at all.
    """
    lead = await _require_lead(session, product, lead_id)
    conversation = await conversation_repo.get_latest_conversation_for_lead(session, lead.id)
    if conversation is None or conversation.status is ConversationStatus.CLOSED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This lead has no open conversation to reply into",
        )
    if await suppression_repo.is_suppressed(session, lead.phone_e164):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This customer has opted out, so they cannot be messaged",
        )
    if not is_within_service_window(
        conversation.last_inbound_at, window_hours=settings.customer_service_window_hours
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"The customer last wrote over {settings.customer_service_window_hours} hours "
                "ago, so WhatsApp only allows an approved template now"
            ),
        )

    if payload.pause_assistant_hours:
        conversation.ai_paused_until = datetime.now(UTC) + timedelta(
            hours=payload.pause_assistant_hours
        )
    await session.commit()

    request_id = str(uuid.uuid4())
    await queue.enqueue(
        TaskRequest(
            task_type=TaskType.SEND_TEAM_REPLY,
            task_id=f"team-reply-{request_id}",
            payload={
                "conversation_id": str(conversation.id),
                "request_id": request_id,
                "body": payload.body.strip(),
                "user_id": str(user.id),
            },
        )
    )
    logger.info(
        "team_reply_queued",
        lead_id=str(lead.id),
        user_id=str(user.id),
        pause_hours=payload.pause_assistant_hours,
    )
    return TeamReplyAccepted(queued=True, ai_paused_until=conversation.ai_paused_until)


@router.post("/leads/{lead_id}/conversation/resume-assistant", response_model=ConversationResponse)
async def resume_assistant(
    lead_id: uuid.UUID, user: CurrentUser, product: ProductScope, session: DbSession
) -> ConversationResponse:
    """Hand the conversation back to the assistant.

    Set to now rather than cleared, so messages that arrived during the pause stay
    the team's: the stranded sweep skips anything older than the pause's end, and
    the assistant answers only what the customer writes next.
    """
    lead = await _require_lead(session, product, lead_id)
    conversation = await conversation_repo.get_latest_conversation_for_lead(session, lead.id)
    if conversation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="This lead has no conversation yet"
        )
    conversation.ai_paused_until = datetime.now(UTC)
    await session.commit()
    logger.info("assistant_resumed", lead_id=str(lead.id), user_id=str(user.id))
    return ConversationResponse.model_validate(conversation)


@router.get("/conversations", response_model=ConversationPage)
async def list_conversations(
    product: ProductScope,
    segment: SegmentFilter,
    session: DbSession,
    conversation_status: ConversationStatus | None = Query(default=None, alias="status"),
    campaign_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
) -> ConversationPage:
    """This product's conversations by most recent activity.

    With a segment, the conversations snapshotted to it. A conversation whose segment
    could not be proven at creation has none, and appears only under all segments.
    """
    total, conversations = await conversation_repo.list_conversations(
        session,
        product_id=product.id,
        segment_id=segment.id if segment is not None else None,
        status=conversation_status,
        campaign_id=campaign_id,
        limit=limit,
        offset=offset,
    )
    summaries = await conversation_repo.summarize_conversations(
        session, [row.id for row in conversations]
    )
    items = []
    for row in conversations:
        summary = summaries.get(row.id, {})
        opted_out = bool(summary.pop("opted_out", False))
        items.append(
            ConversationListItem.model_validate(row).model_copy(
                update={
                    **summary,
                    # An opted-out customer is never someone to answer.
                    "awaiting_reply": not opted_out
                    and summary.get("last_message_direction") == MessageDirection.INBOUND,
                }
            )
        )
    return ConversationPage(total=total, limit=limit, offset=offset, items=items)


@router.get("/conversations/{conversation_id}", response_model=ConversationDetailResponse)
async def get_conversation(
    conversation_id: uuid.UUID,
    product: ProductScope,
    session: DbSession,
    settings: SettingsDep,
    limit: int = Query(default=100, ge=1, le=MAX_PAGE_SIZE),
) -> ConversationDetailResponse:
    conversation = owned_by(
        await conversation_repo.get_conversation(session, conversation_id),
        product,
        not_found="Conversation not found",
    )

    lead = await conversation_repo.get_lead(session, conversation.lead_id)
    if lead is None:
        # The FK cascades, so this means the row was deleted mid-request.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")

    return await _detail(session, settings, conversation, lead, limit)


# ---------------------------------------------------------------- helpers


async def _detail(
    session: DbSession,
    settings: SettingsDep,
    conversation: Conversation,
    lead: Lead,
    limit: int,
) -> ConversationDetailResponse:
    turns = await conversation_repo.get_history(session, conversation.id, limit=limit)
    total = await conversation_repo.count_history(session, conversation.id)

    return ConversationDetailResponse(
        conversation=ConversationResponse.model_validate(conversation),
        lead=LeadResponse.model_validate(lead),
        # Computed per request: it is a function of the clock, and a stored copy would
        # be stale seconds later.
        within_service_window=is_within_service_window(
            conversation.last_inbound_at,
            window_hours=settings.customer_service_window_hours,
        ),
        total_turns=total,
        turns=[ConversationTurnResponse.model_validate(turn) for turn in turns],
    )


async def _require_lead(session: DbSession, product: Product, lead_id: uuid.UUID) -> Lead:
    return owned_by(
        await conversation_repo.get_lead(session, lead_id), product, not_found="Lead not found"
    )


__all__ = ["router"]
