"""Campaign and template queries."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.campaign import META_STATUS_APPROVED, recipient_status_for
from app.domain.enums import CampaignStatus, RecipientStatus, TemplateCategory
from app.persistence.models import (
    Campaign,
    CampaignRecipient,
    Lead,
    WhatsAppSuppression,
    WhatsAppTemplate,
)

# ---------------------------------------------------------------- templates


async def get_template(session: AsyncSession, template_id: uuid.UUID) -> WhatsAppTemplate | None:
    return await session.get(WhatsAppTemplate, template_id)


async def list_approved_marketing_templates(
    session: AsyncSession,
) -> Sequence[WhatsAppTemplate]:
    """Templates a broadcast may use ([D6]).

    Filters on the status Meta reported. Nothing here can mark a template approved;
    rows arrive only from the Phase 6 synchronization.
    """
    result = await session.execute(
        select(WhatsAppTemplate)
        .where(
            WhatsAppTemplate.provider_status == META_STATUS_APPROVED,
            WhatsAppTemplate.category == TemplateCategory.MARKETING,
        )
        .order_by(WhatsAppTemplate.name)
    )
    return result.scalars().all()


# ---------------------------------------------------------------- campaigns


async def create(
    session: AsyncSession,
    *,
    name: str,
    audience_id: uuid.UUID,
    template_id: uuid.UUID,
    created_by_id: uuid.UUID | None,
) -> Campaign:
    campaign = Campaign(
        name=name,
        audience_id=audience_id,
        template_id=template_id,
        created_by_id=created_by_id,
        status=CampaignStatus.DRAFT,
    )
    session.add(campaign)
    await session.flush()
    return campaign


async def get_by_id(session: AsyncSession, campaign_id: uuid.UUID) -> Campaign | None:
    return await session.get(Campaign, campaign_id)


async def list_campaigns(session: AsyncSession) -> Sequence[Campaign]:
    result = await session.execute(select(Campaign).order_by(Campaign.created_at.desc()))
    return result.scalars().all()


# ---------------------------------------------------------------- recipient snapshot


async def build_snapshot(session: AsyncSession, campaign: Campaign) -> int:
    """Freeze the campaign's recipients from its audience (§7).

    The snapshot is what makes a campaign stable: §7 requires that later audience
    changes do not alter a campaign already created. Rows are copied here, including
    the phone number, so nothing downstream re-reads the audience.

    [D16] is applied at snapshot time: a number on the global suppression list is
    recorded SKIPPED rather than PENDING, so it is never enqueued. The send path
    checks again, because a customer can opt out between preparation and sending.

    `ON CONFLICT DO NOTHING` against the unique (campaign_id, lead_id) constraint
    makes this idempotent, which is what §20 requires of campaign recipients: running
    it twice cannot produce a second send to the same lead.
    """
    suppressed = select(WhatsAppSuppression.phone_e164).scalar_subquery()

    rows = (
        await session.execute(
            select(
                Lead.id,
                Lead.phone_e164,
                Lead.phone_e164.in_(suppressed).label("suppressed"),
            )
            .where(Lead.source_audience_id == campaign.audience_id)
            .order_by(Lead.phone_e164)
        )
    ).all()

    if not rows:
        return 0

    statement = pg_insert(CampaignRecipient).values(
        [
            {
                "campaign_id": campaign.id,
                "lead_id": row.id,
                "phone_e164": row.phone_e164,
                "status": recipient_status_for(suppressed=bool(row.suppressed)),
            }
            for row in rows
        ]
    )
    await session.execute(
        statement.on_conflict_do_nothing(
            index_elements=[CampaignRecipient.campaign_id, CampaignRecipient.lead_id]
        )
    )
    await session.flush()

    await refresh_counts(session, campaign)
    return len(rows)


async def count_recipients(
    session: AsyncSession,
    campaign_id: uuid.UUID,
    *,
    status: RecipientStatus | None = None,
) -> int:
    query = (
        select(func.count())
        .select_from(CampaignRecipient)
        .where(CampaignRecipient.campaign_id == campaign_id)
    )
    if status is not None:
        query = query.where(CampaignRecipient.status == status)
    return int((await session.execute(query)).scalar_one())


async def list_recipients(
    session: AsyncSession,
    campaign_id: uuid.UUID,
    *,
    status: RecipientStatus | None = None,
    limit: int = 100,
    offset: int = 0,
) -> Sequence[CampaignRecipient]:
    query = select(CampaignRecipient).where(CampaignRecipient.campaign_id == campaign_id)
    if status is not None:
        query = query.where(CampaignRecipient.status == status)
    query = query.order_by(CampaignRecipient.phone_e164).limit(limit).offset(offset)
    return (await session.execute(query)).scalars().all()


async def list_pending_recipients(
    session: AsyncSession, campaign_id: uuid.UUID
) -> Sequence[CampaignRecipient]:
    """Recipients still awaiting a send.

    Only PENDING rows: a recipient already queued, sent, failed or skipped must not be
    enqueued a second time when a broadcast is re-triggered.
    """
    result = await session.execute(
        select(CampaignRecipient)
        .where(
            CampaignRecipient.campaign_id == campaign_id,
            CampaignRecipient.status == RecipientStatus.PENDING,
        )
        .order_by(CampaignRecipient.phone_e164)
    )
    return result.scalars().all()


async def suppress_recipients_now_opted_out(session: AsyncSession, campaign_id: uuid.UUID) -> int:
    """Re-check suppression immediately before sending ([D16]).

    A customer can opt out between the snapshot and the broadcast. Without this
    re-check the campaign would message someone who had already said stop, which is
    the exact failure [D16] exists to prevent.
    """
    suppressed = select(WhatsAppSuppression.phone_e164).scalar_subquery()

    pending = (
        (
            await session.execute(
                select(CampaignRecipient).where(
                    CampaignRecipient.campaign_id == campaign_id,
                    CampaignRecipient.status == RecipientStatus.PENDING,
                    CampaignRecipient.phone_e164.in_(suppressed),
                )
            )
        )
        .scalars()
        .all()
    )

    for recipient in pending:
        recipient.status = RecipientStatus.SKIPPED

    if pending:
        await session.flush()
    return len(pending)


async def mark_queued(session: AsyncSession, recipients: Sequence[CampaignRecipient]) -> None:
    """Move recipients to QUEUED once their task is accepted.

    Done after the enqueue succeeds, so a queue failure leaves them PENDING and a
    retry picks them up rather than losing them.
    """
    now = datetime.now(UTC)
    for recipient in recipients:
        recipient.status = RecipientStatus.QUEUED
        recipient.queued_at = now
    await session.flush()


async def refresh_counts(session: AsyncSession, campaign: Campaign) -> None:
    """Recompute the campaign's denormalized counters from its recipients.

    Derived, never authoritative: the recipient rows are the truth, and these exist
    so the §17 overview does not aggregate on every page load.
    """
    result = await session.execute(
        select(CampaignRecipient.status, func.count().label("count"))
        .where(CampaignRecipient.campaign_id == campaign.id)
        .group_by(CampaignRecipient.status)
    )
    # Indexed rather than by attribute: `row.count` would resolve to the tuple's
    # own count() method, not the aggregate.
    counts: dict[RecipientStatus, int] = {row[0]: int(row[1]) for row in result.all()}

    campaign.total_recipients = sum(counts.values())
    campaign.sent_count = counts.get(RecipientStatus.SENT, 0)
    campaign.delivered_count = counts.get(RecipientStatus.DELIVERED, 0)
    campaign.read_count = counts.get(RecipientStatus.READ, 0)
    campaign.failed_count = counts.get(RecipientStatus.FAILED, 0)
    campaign.skipped_count = counts.get(RecipientStatus.SKIPPED, 0)
    await session.flush()
