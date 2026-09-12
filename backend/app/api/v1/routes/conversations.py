"""Lead and conversation endpoints, per product (specification §9, §10, §16, §17, §28).

Read-mostly. The dashboard shows what the AI and the customer said and lets a human
correct a lead's details; it does not let anyone type a message into a conversation.
Outbound free-form replies come from the worker, which is where the 24-hour window and
suppression checks live — an endpoint that bypassed them would be a way to message a
customer who has opted out.

Every route is inside one product (Phase 26). One person who enquired about two products
is two leads ([D41]), each with its own address under its own product; nothing here
merges them because the phone number matches. A conversation's product and segment were
snapshotted at creation (§28.5) and are only read here — viewing it from the dashboard
never re-derives either.
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
from app.api.scope import PRODUCT_PREFIX, ProductScope, SegmentFilter, owned_by
from app.core.logging import get_logger
from app.domain.conversation import LeadStateError
from app.domain.enums import ConversationStatus, LeadStatus
from app.domain.whatsapp_webhook import is_within_service_window
from app.persistence.models import Conversation, Lead, Product
from app.persistence.repositories import conversations as conversation_repo

router = APIRouter(prefix=PRODUCT_PREFIX, tags=["conversations"])
logger = get_logger(__name__)

MAX_PAGE_SIZE = 200


@router.get("/leads", response_model=LeadPage)
async def list_leads(
    product: ProductScope,
    segment: SegmentFilter,
    session: DbSession,
    lead_status: LeadStatus | None = Query(default=None, alias="status"),
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


@router.get("/leads/{lead_id}", response_model=LeadResponse)
async def get_lead(lead_id: uuid.UUID, product: ProductScope, session: DbSession) -> LeadResponse:
    return LeadResponse.model_validate(await _require_lead(session, product, lead_id))


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


@router.get("/conversations", response_model=ConversationPage)
async def list_conversations(
    product: ProductScope,
    segment: SegmentFilter,
    session: DbSession,
    conversation_status: ConversationStatus | None = Query(default=None, alias="status"),
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
        limit=limit,
        offset=offset,
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
