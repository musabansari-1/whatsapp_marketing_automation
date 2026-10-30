"""Campaign and template queries."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.domain.campaign import recipient_status_for
from app.domain.enums import (
    CampaignStatus,
    ContactValidationStatus,
    RecipientStatus,
)
from app.persistence.models import (
    Audience,
    AudienceContact,
    Campaign,
    CampaignActivity,
    CampaignRecipient,
    Lead,
    WhatsAppSuppression,
    WhatsAppTemplate,
)
from app.persistence.repositories.scoping import campaign_scope

# ---------------------------------------------------------------- templates


async def get_template(session: AsyncSession, template_id: uuid.UUID) -> WhatsAppTemplate | None:
    return await session.get(WhatsAppTemplate, template_id)


# ---------------------------------------------------------------- campaigns


async def create(
    session: AsyncSession,
    *,
    name: str,
    audience: Audience,
    template_id: uuid.UUID,
    created_by_id: uuid.UUID | None,
) -> Campaign:
    """Create a campaign, taking its product and segment from its audience (§28.6).

    Neither is a choice the caller gets to make. A campaign sends to an audience, the
    audience already belongs to a product and — where it is targeted — to a segment, so
    deriving both is the only answer that cannot disagree with the recipients. It also
    makes a cross-product campaign impossible to express rather than merely rejected.
    """
    campaign = Campaign(
        name=name,
        audience_id=audience.id,
        product_id=audience.product_id,
        segment_id=audience.segment_id,
        template_id=template_id,
        created_by_id=created_by_id,
        status=CampaignStatus.DRAFT,
    )
    session.add(campaign)
    await session.flush()
    return campaign


async def get_by_id(session: AsyncSession, campaign_id: uuid.UUID) -> Campaign | None:
    return await session.get(Campaign, campaign_id)


async def record_activity(session: AsyncSession, campaign_id: uuid.UUID, event_type: str) -> None:
    session.add(
        CampaignActivity(
            campaign_id=campaign_id,
            event_type=event_type,
            created_at=datetime.now(UTC),
        )
    )
    await session.flush()


async def list_activity(session: AsyncSession, campaign_id: uuid.UUID) -> Sequence[CampaignActivity]:
    result = await session.execute(
        select(CampaignActivity)
        .where(CampaignActivity.campaign_id == campaign_id)
        .order_by(CampaignActivity.created_at, CampaignActivity.id)
    )
    return result.scalars().all()


async def list_campaigns(
    session: AsyncSession, *, product_id: uuid.UUID, segment_id: uuid.UUID | None = None,
    include_archived: bool = False,
) -> Sequence[Campaign]:
    """One product's campaigns, newest first. There is no all-products campaign list."""
    conditions = campaign_scope(product_id=product_id, segment_id=segment_id)
    if not include_archived:
        conditions.append(Campaign.archived_at.is_(None))
    result = await session.execute(
        select(Campaign)
        .where(*conditions)
        .order_by(Campaign.created_at.desc())
    )
    campaigns = result.scalars().all()
    # Recipient statuses can change asynchronously as send jobs finish and provider
    # webhooks arrive. Keep the denormalized counters current for the list response.
    for campaign in campaigns:
        await refresh_counts(session, campaign)
    return campaigns


# ---------------------------------------------------------------- recipient snapshot


async def build_snapshot(session: AsyncSession, campaign: Campaign) -> int:
    """Freeze the campaign's recipients from its audience (§7).

    The snapshot is what makes a campaign stable: §7 requires that later audience
    changes do not alter a campaign already created. Rows are copied here, including
    the phone number, so nothing downstream re-reads the audience.

    Recipients are selected through **audience membership** — `audience_contacts
    .lead_id` — and not through `Lead.source_audience_id` ([D55]). The distinction is
    not academic. `source_audience_id` records where a lead was *first* seen and is
    deliberately not updated when the same number appears in a later file, so
    selecting by it silently omitted every lead this audience shared with an earlier
    one. A campaign simply reached fewer people than its audience contained, with no
    error to notice.

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
            .join(AudienceContact, AudienceContact.lead_id == Lead.id)
            .where(
                AudienceContact.audience_id == campaign.audience_id,
                AudienceContact.validation_status == ContactValidationStatus.VALID,
            )
            # Defensive: within one audience a phone yields one VALID row, later
            # repeats being DUPLICATE, so this should never collapse anything today.
            # It costs nothing and stops a change in duplicate handling from quietly
            # producing two recipient rows for one lead.
            .distinct()
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
    # The lead comes along because the dashboard shows who each number belongs to.
    query = (
        select(CampaignRecipient)
        .options(selectinload(CampaignRecipient.lead))
        .where(CampaignRecipient.campaign_id == campaign_id)
    )
    if status is not None:
        query = query.where(CampaignRecipient.status == status)
    query = query.order_by(CampaignRecipient.phone_e164).limit(limit).offset(offset)
    return (await session.execute(query)).scalars().all()


async def recipient_status_counts(
    session: AsyncSession, campaign_id: uuid.UUID
) -> dict[RecipientStatus, int]:
    """How many recipients are in each status, read live from the rows."""
    result = await session.execute(
        select(CampaignRecipient.status, func.count())
        .where(CampaignRecipient.campaign_id == campaign_id)
        .group_by(CampaignRecipient.status)
    )
    return {row[0]: int(row[1]) for row in result.all()}


async def failure_reasons(
    session: AsyncSession, campaign_id: uuid.UUID
) -> list[tuple[str | None, str | None, int]]:
    """Failed recipients grouped by Meta's error, most common first."""
    result = await session.execute(
        select(CampaignRecipient.error_code, CampaignRecipient.error_message, func.count())
        .where(
            CampaignRecipient.campaign_id == campaign_id,
            CampaignRecipient.status == RecipientStatus.FAILED,
        )
        .group_by(CampaignRecipient.error_code, CampaignRecipient.error_message)
        .order_by(func.count().desc())
    )
    return [(row[0], row[1], int(row[2])) for row in result.all()]


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


async def stop_unsent_recipients(session: AsyncSession, campaign_id: uuid.UUID) -> int:
    """Mark every not-yet-sent recipient SKIPPED when a campaign is cancelled ([D29]).

    PENDING and QUEUED only: a recipient already SENT cannot be recalled, and one
    already FAILED or SKIPPED has settled.

    This is belt and braces rather than the whole mechanism — a task already dispatched
    still reaches the worker, which re-checks the campaign state. Marking the rows here
    means the dashboard reflects the cancellation immediately instead of waiting for
    every queued task to drain.
    """
    result = await session.execute(
        update(CampaignRecipient)
        .where(
            CampaignRecipient.campaign_id == campaign_id,
            CampaignRecipient.status.in_([RecipientStatus.PENDING, RecipientStatus.QUEUED]),
        )
        .values(status=RecipientStatus.SKIPPED)
    )
    await session.flush()
    return int(cast("CursorResult[Any]", result).rowcount or 0)


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
    # These are cumulative delivery milestones, not mutually exclusive current
    # statuses: every delivered/read recipient was also sent, and every read
    # recipient was also delivered.
    campaign.sent_count = sum(
        counts.get(status, 0)
        for status in (RecipientStatus.SENT, RecipientStatus.DELIVERED, RecipientStatus.READ)
    )
    campaign.delivered_count = counts.get(RecipientStatus.DELIVERED, 0) + counts.get(
        RecipientStatus.READ, 0
    )
    campaign.read_count = counts.get(RecipientStatus.READ, 0)
    campaign.failed_count = counts.get(RecipientStatus.FAILED, 0)
    campaign.skipped_count = counts.get(RecipientStatus.SKIPPED, 0)
    await session.flush()
