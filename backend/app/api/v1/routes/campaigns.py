"""Campaign endpoints, per product (specification §7, §16, §28, [D6], [D16], [D57]).

A broadcast is human-initiated (§7): creating a campaign prepares it, and a separate
explicit confirmation triggers the send. Sending itself is asynchronous — this module
enqueues work and returns, because §4 forbids long-running work inside a request.

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
    CampaignDetailResponse,
    CampaignRecipientPage,
    CampaignRecipientResponse,
    CampaignResponse,
    CreateCampaignRequest,
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
from app.core.logging import get_logger
from app.domain.campaign import (
    CampaignNotSendableError,
    CampaignStateError,
    TemplateFacts,
    TemplateNotApprovedError,
    TemplateOwnershipError,
    assert_sendable,
    assert_template_owned_by,
    assert_template_usable,
    assert_transition,
)
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
    except (TemplateNotApprovedError, TemplateOwnershipError) as exc:
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
    product: ProductScope, segment: SegmentFilter, session: DbSession
) -> list[CampaignResponse]:
    campaigns = await campaign_repo.list_campaigns(
        session, product_id=product.id, segment_id=segment.id if segment is not None else None
    )
    return [CampaignResponse.model_validate(campaign) for campaign in campaigns]


@router.get("/{campaign_id}", response_model=CampaignDetailResponse)
async def get_campaign(
    campaign_id: uuid.UUID, product: ProductScope, session: DbSession
) -> CampaignDetailResponse:
    return await _detail(session, await _require_campaign(session, product, campaign_id))


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
        items=[CampaignRecipientResponse.model_validate(r) for r in recipients],
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
    """
    campaign = await _require_campaign(session, product, campaign_id)

    # Checked again at the human moment, not only at creation ([D57]). A campaign created
    # before ownership was enforced may point at a template with no product, and this is
    # the last point where refusing costs nothing: nothing has been queued yet.
    template = await campaign_repo.get_template(session, campaign.template_id)
    try:
        assert_template_owned_by(
            template_product_id=template.product_id if template is not None else None,
            campaign_product_id=campaign.product_id,
        )
    except TemplateOwnershipError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    # The broadcast leaves from this product's own number ([D40]). Without one nothing is
    # queued: no other number may stand in, and every recipient would only fail (Phase 27).
    if not product.whatsapp_phone_number_id or not product.whatsapp_business_account_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"{product.display_name} has no WhatsApp number configured, so nothing can "
                "be sent for it."
            ),
        )

    # [D16]: re-check immediately before sending, because a customer can opt out
    # between the snapshot and this moment.
    newly_suppressed = await campaign_repo.suppress_recipients_now_opted_out(session, campaign.id)

    pending = await campaign_repo.list_pending_recipients(session, campaign.id)

    try:
        assert_sendable(campaign.status, len(pending))
    except CampaignNotSendableError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    assert_transition(campaign.status, CampaignStatus.PROCESSING)

    for recipient in pending:
        await queue.enqueue(
            TaskRequest(
                task_type=TaskType.SEND_CAMPAIGN_MESSAGE,
                # Deterministic, so a duplicate enqueue collapses rather than sending
                # twice (§20).
                task_id=f"send-{recipient.id}",
                payload={
                    "campaign_id": str(campaign.id),
                    "recipient_id": str(recipient.id),
                    "lead_id": str(recipient.lead_id),
                },
            )
        )

    # Only after the queue accepted the work: a failure above leaves recipients
    # PENDING so a retry picks them up rather than losing them.
    await campaign_repo.mark_queued(session, pending)

    campaign.status = CampaignStatus.PROCESSING
    campaign.started_at = datetime.now(UTC)
    await campaign_repo.refresh_counts(session, campaign)

    logger.info(
        "campaign_send_confirmed",
        campaign_id=str(campaign.id),
        product_id=str(product.id),
        queued=len(pending),
        skipped_as_suppressed=newly_suppressed,
        by_user_id=str(user.id),
    )
    return SendCampaignResponse(
        campaign=CampaignResponse.model_validate(campaign),
        queued=len(pending),
        skipped_as_suppressed=newly_suppressed,
    )


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
    campaign = await _require_campaign(session, product, campaign_id)

    if campaign.status is CampaignStatus.CANCELLED:
        return CampaignResponse.model_validate(campaign)

    try:
        assert_transition(campaign.status, CampaignStatus.CANCELLED)
    except CampaignStateError as exc:
        # A completed or failed campaign has nothing left to stop.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    stopped = await campaign_repo.stop_unsent_recipients(session, campaign.id)
    campaign.status = CampaignStatus.CANCELLED
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


async def _detail(session: DbSession, campaign: Campaign) -> CampaignDetailResponse:
    template = await campaign_repo.get_template(session, campaign.template_id)
    if template is None:  # pragma: no cover - the foreign key prevents this
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found")

    pending = await campaign_repo.count_recipients(
        session, campaign.id, status=RecipientStatus.PENDING
    )
    return CampaignDetailResponse(
        campaign=CampaignResponse.model_validate(campaign),
        template=WhatsAppTemplateResponse.model_validate(template),
        pending_recipients=pending,
    )


__all__ = ["router"]
