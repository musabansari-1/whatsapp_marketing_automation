"""Starting a broadcast (specification §7, §16, §20, [D16], [D57]).

One function starts a campaign, whoever asks: the "Send broadcast" button, the delayed
task that fires at a scheduled moment, and the sweep that backs that task up. Before
scheduling existed this lived inside the send route; three callers repeating its checks
would drift, and the drift would show up as a scheduled broadcast that skipped a check
the button makes.

The caller owns the transaction and must hold the campaign's row lock
(`campaigns.get_for_update`), so two triggers arriving together cannot both start it.
Nothing here commits.

Idempotent in the same way the route always was: only PENDING recipients are enqueued,
under deterministic task ids, so a repeat start messages nobody twice (§20).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.campaign import (
    CampaignNotSendableError,
    TemplateFacts,
    TemplateMediaMissingError,
    TemplateNotApprovedError,
    TemplateOwnershipError,
    assert_header_media_available,
    assert_sendable,
    assert_template_owned_by,
    assert_template_usable,
    assert_transition,
)
from app.domain.enums import CampaignStatus, TemplateCategory
from app.domain.product import ProductInactiveError, assert_accepts_new_activity
from app.integrations.queue import QueueProvider, TaskRequest, TaskType
from app.persistence.models import Campaign, Product
from app.persistence.repositories import campaigns as campaign_repo


class BroadcastRefusedError(Exception):
    """The campaign cannot start. The message is written for the person who asked."""


@dataclass(frozen=True, slots=True)
class BroadcastStarted:
    queued: int
    skipped_as_suppressed: int


async def check_can_broadcast(session: AsyncSession, campaign: Campaign, product: Product) -> None:
    """Every check that does not change anything, in the order a person would fix them.

    Run at the moment of sending, and also when a campaign is scheduled, so a problem
    is named while someone is looking rather than discovered at the chosen moment.
    """
    if campaign.archived_at is not None:
        raise BroadcastRefusedError("Restore this campaign before sending it")

    try:
        assert_accepts_new_activity(is_active=product.is_active, display_name=product.display_name)
    except ProductInactiveError as exc:
        raise BroadcastRefusedError(str(exc)) from exc

    template = await campaign_repo.get_template(session, campaign.template_id)
    try:
        # Checked again at the moment of sending, not only at creation ([D57]). A campaign
        # created before ownership was enforced may point at a template with no product.
        assert_template_owned_by(
            template_product_id=template.product_id if template is not None else None,
            campaign_product_id=campaign.product_id,
        )
        if template is not None:
            # Meta can pause or reject an approved template after the campaign was made,
            # and a scheduled start can be weeks later. Every recipient would then fail.
            assert_template_usable(
                TemplateFacts(
                    provider_status=template.provider_status,
                    is_marketing=template.category is TemplateCategory.MARKETING,
                )
            )
            assert_header_media_available(
                header_type=template.header_type, media_url=template.header_media_url
            )
    except (TemplateOwnershipError, TemplateNotApprovedError, TemplateMediaMissingError) as exc:
        raise BroadcastRefusedError(str(exc)) from exc

    # The broadcast leaves from this product's own number ([D40]). Without one nothing is
    # queued: no other number may stand in, and every recipient would only fail (Phase 27).
    if not product.whatsapp_phone_number_id or not product.whatsapp_business_account_id:
        raise BroadcastRefusedError(
            f"{product.display_name} has no WhatsApp number configured, so nothing can "
            "be sent for it."
        )


async def start_broadcast(
    session: AsyncSession, queue: QueueProvider, campaign: Campaign, product: Product
) -> BroadcastStarted:
    """Validate, re-check suppression, enqueue one task per pending recipient, and
    move the campaign to PROCESSING. The worker performs the sending (§4, §18)."""
    await check_can_broadcast(session, campaign, product)

    # [D16]: re-check immediately before sending, because a customer can opt out
    # between the snapshot and this moment.
    newly_suppressed = await campaign_repo.suppress_recipients_now_opted_out(session, campaign.id)

    pending = await campaign_repo.list_pending_recipients(session, campaign.id)

    try:
        assert_sendable(campaign.status, len(pending))
    except CampaignNotSendableError as exc:
        raise BroadcastRefusedError(str(exc)) from exc

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
    await campaign_repo.record_activity(session, campaign.id, "PROCESSING")
    await campaign_repo.refresh_counts(session, campaign)

    return BroadcastStarted(queued=len(pending), skipped_as_suppressed=newly_suppressed)


__all__ = [
    "BroadcastRefusedError",
    "BroadcastStarted",
    "check_can_broadcast",
    "start_broadcast",
]
