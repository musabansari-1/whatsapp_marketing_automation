"""Campaign endpoints, per product (specification §7, §16, §28, [D6], [D16], [D57]).

A broadcast is human-initiated (§7): creating a campaign prepares it, and a separate
explicit confirmation triggers the send. Sending itself is asynchronous — this module
enqueues work and returns, because §4 forbids long-running work inside a request.

A broadcast can also be confirmed for a later moment (schedule). It then starts exactly
as "Send broadcast" would — same checks, same code (`app.services.broadcast`) — from a
delayed task at that moment, backed by a sweep that runs every minute, so a lost task
delays the start by at most a minute rather than losing it.

Every route is inside one product (Phase 26). A campaign takes its product from its
audience, which must belong to the product in the path; its template must be one that
product owns. So the address, the audience and the template cannot disagree about which
product is broadcasting — the failure that would send one product's message to another
product's customers.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import AdminUser, DbSession, Queue
from app.api.schemas import (
    CampaignActivityResponse,
    CampaignDetailResponse,
    CampaignFailureReason,
    CampaignRecipientPage,
    CampaignRecipientResponse,
    CampaignResponse,
    CreateCampaignRequest,
    ScheduleCampaignRequest,
    SendCampaignResponse,
    WhatsAppTemplateResponse,
)
from app.api.scope import (
    PRODUCT_PREFIX,
    ActiveProductScope,
    ProductScope,
    SegmentFilter,
    owned_by,
)
from app.core.errors import ProviderError
from app.core.logging import get_logger
from app.domain.campaign import (
    CampaignStateError,
    TemplateFacts,
    TemplateMediaMissingError,
    TemplateNotApprovedError,
    TemplateOwnershipError,
    assert_header_media_available,
    assert_template_owned_by,
    assert_template_usable,
    assert_transition,
)
from app.domain.campaign_schedule import ScheduleTimeError, assert_schedulable_time
from app.domain.enums import (
    AudienceStatus,
    CampaignStatus,
    RecipientStatus,
    TemplateCategory,
)
from app.integrations.queue import TaskRequest, TaskType
from app.persistence.models import Campaign, Product
from app.persistence.repositories import audiences as audience_repo
from app.persistence.repositories import campaigns as campaign_repo
from app.services.broadcast import BroadcastRefusedError, check_can_broadcast, start_broadcast

router = APIRouter(prefix=f"{PRODUCT_PREFIX}/campaigns", tags=["campaigns"])
logger = get_logger(__name__)


@router.post("", response_model=CampaignDetailResponse, status_code=status.HTTP_201_CREATED)
async def create_campaign(
    payload: CreateCampaignRequest,
    user: AdminUser,
    product: ActiveProductScope,
    session: DbSession,
) -> CampaignDetailResponse:
    """Create a campaign in this product and freeze its recipients.

    ADMIN-only ([D33]).

    The audience must belong to this product — another product's is 404, exactly as if
    it did not exist. The template must be approved by Meta and owned by this product;
    an unassigned template is refused ([D57]).

    The snapshot is taken now rather than at send time, so the preview the admin
    confirms against is exactly the set that will be messaged (§7). Nothing is sent.
    """
    audience = owned_by(
        await audience_repo.get_by_id(session, payload.audience_id),
        product,
        not_found="Audience not found",
    )
    if audience.archived_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Restore this audience before creating a campaign",
        )
    if audience.status is not AudienceStatus.CONFIRMED:
        # §6 puts admin confirmation before use; an unconfirmed audience has not been
        # reviewed and its leads do not exist yet.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="This audience has not been confirmed yet",
        )

    template = await campaign_repo.get_template(session, payload.template_id)
    if template is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found")

    try:
        assert_template_usable(
            TemplateFacts(
                provider_status=template.provider_status,
                is_marketing=template.category is TemplateCategory.MARKETING,
            )
        )
        # §28.8 and [D57]. The campaign's product is the audience's, which is this
        # product — checked against the value it will be given.
        assert_template_owned_by(
            template_product_id=template.product_id, campaign_product_id=product.id
        )
        # A media-header template with no asset cannot be sent, and Meta's refusal
        # (132012) is permanent and silent about the cause. Refusing at creation names
        # the problem while there is still a human to fix it, rather than after a
        # confirmed broadcast has failed on every recipient.
        assert_header_media_available(
            header_type=template.header_type, media_url=template.header_media_url
        )
    except (TemplateNotApprovedError, TemplateOwnershipError, TemplateMediaMissingError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    campaign = await campaign_repo.create(
        session,
        name=payload.name,
        audience=audience,
        template_id=payload.template_id,
        created_by_id=user.id,
    )

    snapshot_size = await campaign_repo.build_snapshot(session, campaign)
    pending = await campaign_repo.count_recipients(
        session, campaign.id, status=RecipientStatus.PENDING
    )

    if pending > 0:
        assert_transition(campaign.status, CampaignStatus.READY)
        campaign.status = CampaignStatus.READY
        await session.flush()

    await campaign_repo.record_activity(session, campaign.id, "CREATED")
    if campaign.status is CampaignStatus.READY:
        await campaign_repo.record_activity(session, campaign.id, "READY")

    logger.info(
        "campaign_created",
        campaign_id=str(campaign.id),
        product_id=str(product.id),
        recipients=snapshot_size,
        pending=pending,
        status=campaign.status.value,
    )
    return await _detail(session, campaign)


@router.get("", response_model=list[CampaignResponse])
async def list_campaigns(
    product: ProductScope,
    segment: SegmentFilter,
    session: DbSession,
    include_archived: bool = Query(default=False),
) -> list[CampaignResponse]:
    campaigns = await campaign_repo.list_campaigns(
        session,
        product_id=product.id,
        segment_id=segment.id if segment is not None else None,
        include_archived=include_archived,
    )
    return [CampaignResponse.model_validate(campaign) for campaign in campaigns]


@router.get("/{campaign_id}", response_model=CampaignDetailResponse)
async def get_campaign(
    campaign_id: uuid.UUID, product: ProductScope, session: DbSession
) -> CampaignDetailResponse:
    return await _detail(session, await _require_campaign(session, product, campaign_id))


@router.post("/{campaign_id}/archive", response_model=CampaignResponse)
async def archive_campaign(
    campaign_id: uuid.UUID, user: AdminUser, product: ProductScope, session: DbSession
) -> CampaignResponse:
    campaign = await _require_campaign(session, product, campaign_id)
    if campaign.status in {
        CampaignStatus.SCHEDULED,
        CampaignStatus.PROCESSING,
        CampaignStatus.PAUSED,
    }:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cancel or finish this campaign before archiving it",
        )
    campaign.archived_at = campaign.archived_at or datetime.now(UTC)
    await session.flush()
    return CampaignResponse.model_validate(campaign)


@router.post("/{campaign_id}/restore", response_model=CampaignResponse)
async def restore_campaign(
    campaign_id: uuid.UUID, user: AdminUser, product: ProductScope, session: DbSession
) -> CampaignResponse:
    campaign = await _require_campaign(session, product, campaign_id)
    campaign.archived_at = None
    await session.flush()
    return CampaignResponse.model_validate(campaign)


@router.get("/{campaign_id}/recipients", response_model=CampaignRecipientPage)
async def list_recipients(
    campaign_id: uuid.UUID,
    product: ProductScope,
    session: DbSession,
    recipient_status: RecipientStatus | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> CampaignRecipientPage:
    """The frozen recipient snapshot, paged and filterable.

    Derived from §7's snapshot requirement: without a way to read it, an admin cannot
    see who a campaign will message or why someone was skipped.
    """
    await _require_campaign(session, product, campaign_id)

    recipients = await campaign_repo.list_recipients(
        session, campaign_id, status=recipient_status, limit=limit, offset=offset
    )
    total = await campaign_repo.count_recipients(session, campaign_id, status=recipient_status)

    return CampaignRecipientPage(
        total=total,
        limit=limit,
        offset=offset,
        items=[
            CampaignRecipientResponse.model_validate(r).model_copy(
                update={"full_name": r.lead.full_name if r.lead else None}
            )
            for r in recipients
        ],
    )


@router.post("/{campaign_id}/send", response_model=SendCampaignResponse)
async def send_campaign(
    campaign_id: uuid.UUID,
    user: AdminUser,
    product: ActiveProductScope,
    session: DbSession,
    queue: Queue,
) -> SendCampaignResponse:
    """Confirm Send Broadcast (§7).

    ADMIN-only ([D33]): a salesperson must never be able to trigger a mass broadcast,
    accidentally or otherwise. Refused for an inactive product.

    This is the human-initiated moment the specification requires. It validates,
    re-checks suppression, enqueues one task per pending recipient and returns; the
    worker performs the actual sending (§4, §18).

    Idempotent: only PENDING recipients are enqueued, so re-triggering a broadcast
    cannot message anyone twice. Each task carries a deterministic ID derived from the
    recipient, so a redelivered task is recognizable as a repeat (§20).

    A SCHEDULED campaign may be sent now too: it is the same confirmed broadcast with
    its moment brought forward. The row lock means its scheduled trigger, arriving
    later, finds it already started and does nothing.
    """
    campaign = await _require_campaign_locked(session, product, campaign_id)
    try:
        started = await start_broadcast(session, queue, campaign, product)
    except BroadcastRefusedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    logger.info(
        "campaign_send_confirmed",
        campaign_id=str(campaign.id),
        product_id=str(product.id),
        queued=started.queued,
        skipped_as_suppressed=started.skipped_as_suppressed,
        by_user_id=str(user.id),
    )
    return SendCampaignResponse(
        campaign=CampaignResponse.model_validate(campaign),
        queued=started.queued,
        skipped_as_suppressed=started.skipped_as_suppressed,
    )


@router.post("/{campaign_id}/schedule", response_model=CampaignResponse)
async def schedule_campaign(
    campaign_id: uuid.UUID,
    payload: ScheduleCampaignRequest,
    user: AdminUser,
    product: ActiveProductScope,
    session: DbSession,
    queue: Queue,
) -> CampaignResponse:
    """Confirm a broadcast for a later moment. ADMIN-only ([D33]), like sending.

    This is the human confirmation §7 requires, given in advance. Every check the send
    makes that changes nothing runs now, so a problem is named while someone is looking.
    They all run again at the moment, because the world can change in between.

    Scheduling an already-scheduled campaign moves it. Nothing is sent here.
    """
    campaign = await _require_campaign_locked(session, product, campaign_id)

    try:
        assert_schedulable_time(payload.scheduled_for, datetime.now(UTC))
    except ScheduleTimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    if campaign.status not in (CampaignStatus.READY, CampaignStatus.SCHEDULED):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Only a READY campaign can be scheduled; this one is {campaign.status.value}",
        )

    try:
        await check_can_broadcast(session, campaign, product)
    except BroadcastRefusedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    pending = await campaign_repo.count_recipients(
        session, campaign.id, status=RecipientStatus.PENDING
    )
    if pending == 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This campaign has no recipients left to send to",
        )

    when = payload.scheduled_for.astimezone(UTC)
    moved = campaign.status is CampaignStatus.SCHEDULED
    if not moved:
        assert_transition(campaign.status, CampaignStatus.SCHEDULED)
        campaign.status = CampaignStatus.SCHEDULED
    campaign.scheduled_for = when
    await campaign_repo.record_activity(
        session, campaign.id, "RESCHEDULED" if moved else "SCHEDULED"
    )
    await session.flush()

    # The exact-time trigger. Its name carries the moment, so moving a schedule creates a
    # new task instead of colliding with the old one, which fires, finds a different
    # moment (or a withdrawn schedule) and does nothing. A failure here is logged, not
    # raised: the schedule is saved, and the minute sweep starts it on time regardless.
    try:
        await queue.enqueue(
            TaskRequest(
                task_type=TaskType.START_SCHEDULED_CAMPAIGN,
                task_id=f"start-{campaign.id}-{int(when.timestamp())}",
                payload={"campaign_id": str(campaign.id)},
                scheduled_for=when,
            )
        )
    except ProviderError as exc:
        logger.warning(
            "campaign_schedule_task_not_enqueued",
            campaign_id=str(campaign.id),
            error=str(exc),
        )

    logger.info(
        "campaign_scheduled",
        campaign_id=str(campaign.id),
        product_id=str(product.id),
        scheduled_for=when.isoformat(),
        pending=pending,
        moved=moved,
        by_user_id=str(user.id),
    )
    return CampaignResponse.model_validate(campaign)


@router.post("/{campaign_id}/unschedule", response_model=CampaignResponse)
async def unschedule_campaign(
    campaign_id: uuid.UUID, user: AdminUser, product: ProductScope, session: DbSession
) -> CampaignResponse:
    """Withdraw a schedule; the campaign goes back to READY. ADMIN-only.

    Allowed for an inactive product, like cancelling: it stops activity. The delayed
    task still fires at the old moment and does nothing, because the campaign is no
    longer SCHEDULED.

    Idempotent: a READY campaign is returned unchanged.
    """
    campaign = await _require_campaign_locked(session, product, campaign_id)

    if campaign.status is CampaignStatus.READY:
        return CampaignResponse.model_validate(campaign)
    if campaign.status is not CampaignStatus.SCHEDULED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Only a scheduled campaign can be unscheduled; this one is {campaign.status.value}"
            ),
        )

    assert_transition(campaign.status, CampaignStatus.READY)
    campaign.status = CampaignStatus.READY
    campaign.scheduled_for = None
    await campaign_repo.record_activity(session, campaign.id, "UNSCHEDULED")
    await session.flush()

    logger.info("campaign_unscheduled", campaign_id=str(campaign.id), by_user_id=str(user.id))
    return CampaignResponse.model_validate(campaign)


@router.post("/{campaign_id}/cancel", response_model=CampaignResponse)
async def cancel_campaign(
    campaign_id: uuid.UUID, user: AdminUser, product: ProductScope, session: DbSession
) -> CampaignResponse:
    """Cancel a campaign ([D29]). ADMIN-only.

    Allowed for an inactive product: cancelling stops activity, and refusing it would
    keep alive exactly what deactivation was meant to end.

    Cancelling stops work that is already queued. Tasks dispatched before this point
    still arrive at the worker, which re-reads the campaign state and declines to
    send — so cancellation is effective without needing to purge the queue.

    Idempotent: cancelling an already-cancelled campaign succeeds and changes nothing.
    Messages already delivered cannot be recalled; only unsent recipients are stopped.
    """
    campaign = await _require_campaign_locked(session, product, campaign_id)

    if campaign.status is CampaignStatus.CANCELLED:
        return CampaignResponse.model_validate(campaign)

    try:
        assert_transition(campaign.status, CampaignStatus.CANCELLED)
    except CampaignStateError as exc:
        # A completed or failed campaign has nothing left to stop.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    stopped = await campaign_repo.stop_unsent_recipients(session, campaign.id)
    campaign.status = CampaignStatus.CANCELLED
    await campaign_repo.record_activity(session, campaign.id, "CANCELLED")
    await campaign_repo.refresh_counts(session, campaign)

    logger.info(
        "campaign_cancelled",
        campaign_id=str(campaign.id),
        recipients_stopped=stopped,
        by_user_id=str(user.id),
    )
    return CampaignResponse.model_validate(campaign)


async def _require_campaign(
    session: DbSession, product: Product, campaign_id: uuid.UUID
) -> Campaign:
    return owned_by(
        await campaign_repo.get_by_id(session, campaign_id), product, not_found="Campaign not found"
    )


async def _require_campaign_locked(
    session: DbSession, product: Product, campaign_id: uuid.UUID
) -> Campaign:
    """The campaign with its row locked, for any route that changes its status."""
    return owned_by(
        await campaign_repo.get_for_update(session, campaign_id),
        product,
        not_found="Campaign not found",
    )


async def _detail(session: DbSession, campaign: Campaign) -> CampaignDetailResponse:
    # Delivery/read webhooks update recipient rows asynchronously. Recompute before
    # serializing so the detail view uses the same source of truth as the list.
    await campaign_repo.refresh_counts(session, campaign)
    template = await campaign_repo.get_template(session, campaign.template_id)
    if template is None:  # pragma: no cover - the foreign key prevents this
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found")

    counts = await campaign_repo.recipient_status_counts(session, campaign.id)
    reasons = await campaign_repo.failure_reasons(session, campaign.id)
    return CampaignDetailResponse(
        campaign=CampaignResponse.model_validate(campaign),
        template=WhatsAppTemplateResponse.model_validate(template),
        pending_recipients=counts.get(RecipientStatus.PENDING, 0),
        activity=[
            CampaignActivityResponse.model_validate(event)
            for event in await campaign_repo.list_activity(session, campaign.id)
        ],
        status_counts=counts,
        failure_reasons=[
            CampaignFailureReason(error_code=code, error_message=message, count=count)
            for code, message, count in reasons
        ],
    )


__all__ = ["router"]
