"""Starting scheduled campaigns at their moment.

Two triggers, one outcome:

- `start_scheduled_campaign` — a delayed task enqueued when the campaign is scheduled,
  timed for the chosen moment. The precise trigger.
- `sweep_scheduled_campaigns` — every minute, every SCHEDULED campaign whose moment has
  passed. The safety net: a delayed task can be refused by the queue, lost, or never
  created (the enqueue at scheduling time is allowed to fail), and none of those may
  cost a broadcast. With the sweep, the worst case is a start one interval late.

Both lock the campaign row and re-read its status before acting, then start it through
`app.services.broadcast.start_broadcast` — the same code "Send broadcast" runs. So the
two triggers can arrive together, twice, or out of order, and the campaign starts once.
A trigger that finds the campaign withdrawn, moved later, cancelled or already started
does nothing.

When the moment arrives and the campaign cannot start, it is handed back to a person
rather than left SCHEDULED to be retried forever: it returns to READY, the reason is
recorded in its activity, and every internal user is notified. That covers a check
failing at the moment (template paused by Meta, number removed, nobody left after
opt-outs) and the lateness limit (every trigger was down long enough that sending now
would be a different message). A transient failure — the queue or database briefly
unavailable — is raised instead, so the transaction rolls back, the campaign stays
SCHEDULED, and the next attempt starts it.
"""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.domain.campaign import assert_transition
from app.domain.campaign_schedule import is_due, is_too_late
from app.domain.enums import CampaignStatus, NotificationType
from app.integrations.queue import TaskRequest
from app.integrations.registry import build_queue_provider
from app.persistence.database import get_session_factory
from app.persistence.models import Campaign, Notification, Product
from app.persistence.repositories import bookings as notification_repo
from app.persistence.repositories import campaigns as campaign_repo
from app.services.broadcast import BroadcastRefusedError, start_broadcast

logger = get_logger(__name__)

#: `related_entity_type` on the bell, so the dashboard can link to the campaign.
ENTITY_CAMPAIGN = "campaign"

# Outcomes, as returned and counted. Strings rather than an enum: they only ever reach
# logs and task results.
STARTED = "started"
NOT_SCHEDULED = "not_scheduled"
NOT_DUE = "not_due"
LOCKED = "locked_elsewhere"
REFUSED = "refused"
MISSED = "missed"


async def start_scheduled_campaign(task: TaskRequest) -> dict[str, int | str]:
    """Start one campaign at its scheduled moment, if it is still due."""
    try:
        campaign_id = uuid.UUID(str(task.payload.get("campaign_id")))
    except ValueError:
        # Retrying cannot fix a malformed payload. The sweep still covers the campaign.
        logger.error("scheduled_campaign_bad_payload", payload=task.payload)
        return {"result": "invalid_payload"}

    outcome = await start_if_due(campaign_id, skip_locked=False)
    return {"result": outcome, "campaign_id": str(campaign_id)}


async def sweep_scheduled_campaigns(task: TaskRequest) -> dict[str, int | str]:
    """Start every SCHEDULED campaign whose moment has passed."""
    del task  # Scheduled with an empty payload.
    settings = get_settings()
    factory = get_session_factory()
    async with factory() as session:
        due = await campaign_repo.list_due_scheduled_ids(
            session, now=datetime.now(UTC), limit=settings.sweep_batch_size
        )

    outcomes: Counter[str] = Counter()
    for campaign_id in due:
        try:
            # skip_locked: a campaign another trigger is starting right now is left to it.
            outcomes[await start_if_due(campaign_id, skip_locked=True)] += 1
        except Exception:  # noqa: BLE001 - one campaign must not stop the others
            # Rolled back and still SCHEDULED, so the next run tries it again.
            logger.exception("scheduled_campaign_start_errored", campaign_id=str(campaign_id))
            outcomes["errored"] += 1

    if due:
        logger.info("scheduled_campaigns_swept", due=len(due), **outcomes)
    return {"result": "swept", "due": len(due), **outcomes}


async def start_if_due(campaign_id: uuid.UUID, *, skip_locked: bool) -> str:
    """Lock the campaign, decide, and start it or hand it back. One transaction."""
    settings = get_settings()
    max_lateness = timedelta(minutes=settings.campaign_schedule_max_lateness_minutes)
    factory = get_session_factory()

    async with factory() as session:
        campaign = await campaign_repo.get_for_update(session, campaign_id, skip_locked=skip_locked)
        if campaign is None:
            # Missing, or (with skip_locked) held by the trigger starting it now.
            return LOCKED if skip_locked else NOT_SCHEDULED
        if campaign.status is not CampaignStatus.SCHEDULED:
            # Withdrawn, cancelled, or already started by the other trigger or a person.
            return NOT_SCHEDULED

        now = datetime.now(UTC)
        scheduled_for = campaign.scheduled_for
        if scheduled_for is None or not is_due(scheduled_for, now):
            # An old task for a schedule that was moved later. The new moment has its
            # own task, and the sweep backs it.
            return NOT_DUE

        product = await session.get(Product, campaign.product_id)
        if product is None:  # pragma: no cover - the foreign key prevents this
            raise ValueError(f"Campaign {campaign.id} has no product")

        if is_too_late(scheduled_for, now, max_lateness):
            late_minutes = int((now - scheduled_for).total_seconds() // 60)
            await _hand_back(
                session,
                campaign,
                product,
                activity="SCHEDULE_MISSED",
                reason=(
                    f"It was due {late_minutes} minutes ago, more than the "
                    f"{settings.campaign_schedule_max_lateness_minutes}-minute limit, so it was "
                    "not sent late. Nothing was sent; schedule it again or send it now."
                ),
            )
            await session.commit()
            logger.warning(
                "scheduled_campaign_missed",
                campaign_id=str(campaign.id),
                late_minutes=late_minutes,
            )
            return MISSED

        try:
            started = await start_broadcast(
                session, build_queue_provider(settings), campaign, product
            )
        except BroadcastRefusedError as exc:
            await _hand_back(
                session,
                campaign,
                product,
                activity="SCHEDULE_FAILED",
                reason=f"{exc} Nothing was sent; fix this, then schedule it again or send it now.",
            )
            await session.commit()
            logger.warning(
                "scheduled_campaign_refused", campaign_id=str(campaign.id), reason=str(exc)
            )
            return REFUSED

        await session.commit()

    logger.info(
        "scheduled_campaign_started",
        campaign_id=str(campaign_id),
        queued=started.queued,
        skipped_as_suppressed=started.skipped_as_suppressed,
    )
    return STARTED


async def _hand_back(
    session: AsyncSession,
    campaign: Campaign,
    product: Product,
    *,
    activity: str,
    reason: str,
) -> None:
    """Return the campaign to READY and tell every internal user why."""
    # A start refused after the suppression re-check may have left partial changes in
    # the session (newly opted-out recipients marked skipped). Those are true facts and
    # are kept; only the status decision is made here.
    assert_transition(campaign.status, CampaignStatus.READY)
    campaign.status = CampaignStatus.READY
    when = campaign.scheduled_for
    campaign.scheduled_for = None
    await campaign_repo.record_activity(session, campaign.id, activity)

    # One bell per campaign per user is all the notification table allows. A campaign
    # that fails, is rescheduled and fails again must still ring, so the earlier bell
    # for this campaign is replaced by the new one with the current reason.
    await session.execute(
        delete(Notification).where(
            Notification.type == NotificationType.CAMPAIGN_SCHEDULE_FAILED,
            Notification.related_entity_type == ENTITY_CAMPAIGN,
            Notification.related_entity_id == campaign.id,
        )
    )
    # The team reads times in the business timezone, the one demos are offered in.
    zone = ZoneInfo(get_settings().demo_timezone)
    scheduled = (
        when.astimezone(zone).strftime(f"%d %b %H:%M ({zone.key})")
        if when
        else "its scheduled time"
    )
    await notification_repo.fan_out_notification(
        session,
        notification_type=NotificationType.CAMPAIGN_SCHEDULE_FAILED,
        title=f"Scheduled campaign did not start — {product.display_name}",
        message=f'"{campaign.name}" was scheduled for {scheduled} and did not start. {reason}',
        entity_type=ENTITY_CAMPAIGN,
        entity_id=campaign.id,
        product_id=campaign.product_id,
    )
    await session.flush()


__all__ = [
    "ENTITY_CAMPAIGN",
    "start_if_due",
    "start_scheduled_campaign",
    "sweep_scheduled_campaigns",
]
