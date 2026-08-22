"""Lead and conversation endpoints (specification §9, §10, §16, §17).

Read-mostly. The dashboard shows what the AI and the customer said and lets a human
correct a lead's details; it does not let anyone type a message into a conversation.
Outbound free-form replies come from the worker, which is where the 24-hour window and
suppression checks live — an endpoint that bypassed them would be a way to message a
customer who has opted out.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import CurrentUser, DbSession, SettingsDep
from app.api.schemas import (
    ConversationDetailResponse,
    ConversationPage,
    ConversationResponse,
    ConversationTurnResponse,
    LeadPage,
    LeadResponse,
    UpdateLeadRequest,
)
from app.core.logging import get_logger
from app.domain.conversation import LeadStateError
from app.domain.enums import ConversationStatus, LeadStatus
from app.domain.whatsapp_webhook import is_within_service_window
from app.persistence.models import Conversation, Lead
from app.persistence.repositories import conversations as conversation_repo

router = APIRouter(tags=["conversations"])
logger = get_logger(__name__)

MAX_PAGE_SIZE = 200


@router.get("/leads", response_model=LeadPage)
async def list_leads(
    user: CurrentUser,
    session: DbSession,
    lead_status: LeadStatus | None = Query(default=None, alias="status"),
    search: str | None = Query(default=None, max_length=255),
    limit: int = Query(default=50, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
) -> LeadPage:
    total, leads = await conversation_repo.list_leads(
        session, status=lead_status, search=search, limit=limit, offset=offset
    )
    return LeadPage(
        total=total,
        limit=limit,
        offset=offset,
        items=[LeadResponse.model_validate(lead) for lead in leads],
    )


@router.get("/leads/{lead_id}", response_model=LeadResponse)
async def get_lead(lead_id: uuid.UUID, user: CurrentUser, session: DbSession) -> LeadResponse:
    return LeadResponse.model_validate(await _require_lead(session, lead_id))


@router.patch("/leads/{lead_id}", response_model=LeadResponse)
async def update_lead(
    lead_id: uuid.UUID,
    payload: UpdateLeadRequest,
    user: CurrentUser,
    session: DbSession,
) -> LeadResponse:
    """Correct a lead's details, and optionally its status.

    A status change here goes through the same §10 transition table the worker uses.
    That is deliberate: [D18] says the *backend* owns `lead.status`, not that no human
    may ever correct it — but a human correcting it must not be able to reach a state
    the specification does not allow either.
    """
    lead = await _require_lead(session, lead_id)

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
    user: CurrentUser,
    session: DbSession,
    settings: SettingsDep,
    limit: int = Query(default=100, ge=1, le=MAX_PAGE_SIZE),
) -> ConversationDetailResponse:
    """The lead's most recent conversation, open or closed.

    Most recent rather than open: [D36] ends a conversation permanently, and a lead
    whose thread closed still has a history worth reading.
    """
    lead = await _require_lead(session, lead_id)
    conversation = await conversation_repo.get_latest_conversation_for_lead(session, lead.id)
    if conversation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="This lead has no conversation yet",
        )

    return await _detail(session, settings, conversation, lead, limit)


@router.get("/conversations", response_model=ConversationPage)
async def list_conversations(
    user: CurrentUser,
    session: DbSession,
    conversation_status: ConversationStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
) -> ConversationPage:
    """Conversations by most recent activity.

    Not in §16's list, which names only the two lookups below. Added because §17's
    conversation view needs something to list, and a dashboard that can only fetch a
    conversation whose ID you already know is not usable.
    """
    total, conversations = await conversation_repo.list_conversations(
        session, status=conversation_status, limit=limit, offset=offset
    )
    return ConversationPage(
        total=total,
        limit=limit,
        offset=offset,
        items=[ConversationResponse.model_validate(row) for row in conversations],
    )


@router.get("/conversations/{conversation_id}", response_model=ConversationDetailResponse)
async def get_conversation(
    conversation_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    settings: SettingsDep,
    limit: int = Query(default=100, ge=1, le=MAX_PAGE_SIZE),
) -> ConversationDetailResponse:
    conversation = await conversation_repo.get_conversation(session, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")

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


async def _require_lead(session: DbSession, lead_id: uuid.UUID) -> Lead:
    lead = await conversation_repo.get_lead(session, lead_id)
    if lead is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lead not found")
    return lead


__all__ = ["router"]
