"""Bot-activity audit sweep ([D67]).

Once an hour, look at recent inbound traffic and flag conversations whose pattern
looks automated — sustained pace with machine-regular gaps, or a marathon history.
The staging incident this answers took twelve days to notice on the token bill:
900 messages from one number, every reply answered, nobody the wiser.

Detection only at first sight, escalation on repeat. Flagging writes one dashboard
notification per suspect conversation for a human to review. An unreviewed repeat
— still suspect an hour later with the first flag unread by anyone — escalates to
an operator-grade block on its own (`MANUAL` suppression, the same row
`block-sender` writes): the team had its chance to look, and the burn continues
meanwhile. A read flag means a human owns the decision and is never overridden.
Nothing here pauses or answers a live reply — so a wrong guess costs a dismissed
notification rather than a lost customer, and an owner load-testing one
conversation simply dismisses (or unblocks) the flag.

Repeats collapse rather than re-announce: the flag's unique constraint is
(user, type, entity), so every hourly run re-flags the same conversation into
zero new rows. Suppressed senders are invisible to the audit — blocked or opted
out means already handled, and flagging them again would nag about a decided case.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.config import get_settings
from app.core.logging import get_logger
from app.domain.bot_audit import BotSignals, BotThresholds, judge, median_gap_seconds
from app.domain.enums import NotificationType
from app.integrations.queue import TaskRequest
from app.persistence.database import get_session_factory
from app.persistence.models import Product
from app.persistence.repositories import bookings as booking_repo
from app.persistence.repositories import bot_audit as bot_audit_repo
from app.persistence.repositories import suppressions as suppression_repo
from app.persistence.repositories import webhooks as webhook_repo
from app.persistence.repositories.bookings import ENTITY_LEAD

logger = get_logger(__name__)

#: The audit always scores this trailing window. An hour matches the cadence: each
#: run sees the hour since the last one, and a marathon sustains across runs while
#: a burst confined to one hour still trips exactly once.
RATE_WINDOW = timedelta(hours=1)


async def sweep_bot_activity(task: TaskRequest) -> dict[str, int | str]:
    """Flag conversations with automated-looking inbound patterns."""
    del task  # Scheduled with an empty payload; thresholds come from settings.
    settings = get_settings()
    thresholds = BotThresholds(
        msgs_per_hour=settings.bot_audit_msgs_per_hour,
        max_median_gap_seconds=settings.bot_audit_max_median_gap_seconds,
        min_lifetime_messages=settings.bot_audit_min_lifetime_messages,
    )

    factory = get_session_factory()
    async with factory() as session:
        activities = await bot_audit_repo.recent_activity(
            session, since=datetime.now(UTC) - RATE_WINDOW
        )

        flagged = 0
        notified = 0
        blocked = 0
        for activity in activities:
            verdict = judge(
                BotSignals(
                    conversation_id=str(activity.conversation_id),
                    window_messages=len(activity.window_starts),
                    window_starts=activity.window_starts,
                    lifetime_messages=activity.lifetime_messages,
                ),
                thresholds,
            )
            if not verdict.suspect:
                continue
            flagged += 1
            # Read BEFORE this run's fan-out: a pre-existing unread flag means the
            # team has not looked while the pattern continued, and that escalates
            # to a block. A read flag means a human owns the decision — never
            # overridden — and a first flag is for review, not action.
            unreviewed = await bot_audit_repo.has_unread_flag(session, lead_id=activity.lead_id)
            product = await session.get(Product, activity.product_id)
            gap = median_gap_seconds(activity.window_starts)
            notified += await booking_repo.fan_out_notification(
                session,
                notification_type=NotificationType.BOT_SUSPECTED,
                title=_notification_title(product),
                message=_notification_message(
                    activity.phone_e164,
                    window_messages=len(activity.window_starts),
                    median_gap_seconds=gap,
                    lifetime_messages=activity.lifetime_messages,
                    reasons=verdict.reasons,
                ),
                entity_type=ENTITY_LEAD,
                entity_id=activity.lead_id,
                product_id=activity.product_id,
            )
            if unreviewed and settings.bot_block_on_repeat:
                # Same row block-sender writes, same consequences: the inbound
                # path drops this sender before any lead, message or turn exists.
                # No mirror, no CRM, no ack — an operator-grade block, automated.
                await suppression_repo.record_manual_block(
                    session,
                    phone_e164=activity.phone_e164,
                    notes="bot audit: unreviewed repeat flag",
                )
                await webhook_repo.cancel_pending_sends(session, activity.phone_e164)
                blocked += 1
                logger.warning(
                    "bot_sender_auto_blocked",
                    conversation_id=str(activity.conversation_id),
                    lead_id=str(activity.lead_id),
                )
        await session.commit()

    if flagged:
        # WARNING on purpose, matching the unanswered-sweep precedent: a suspect
        # existing at all is worth noticing even though flagging itself worked.
        logger.warning(
            "bot_audit_suspected",
            conversations=flagged,
            notified=notified,
            blocked=blocked,
        )
    else:
        logger.info("bot_audit_clean", conversations=len(activities))
    return {
        "result": "audited",
        "conversations": len(activities),
        "flagged": flagged,
        "notified": notified,
        "blocked": blocked,
    }


def _notification_title(product: Product | None) -> str:
    """The bell's headline: what it is and whose product, never an accusation."""
    if product is not None:
        return f"Possible automated sender — {product.display_name}"
    return "Possible automated sender"


def _notification_message(
    phone_e164: str,
    *,
    window_messages: int,
    median_gap_seconds: float | None,
    lifetime_messages: int,
    reasons: tuple[str, ...],
) -> str:
    """What a reviewer needs: who, the shape that tripped, and what to do."""
    gap = f"{median_gap_seconds:.0f}s median gap" if median_gap_seconds is not None else "n/a"
    detail = "; ".join(reasons)
    return (
        f"{phone_e164} sent {window_messages} messages in the last hour ({gap}, "
        f"{lifetime_messages} lifetime: {detail}). Pattern looks automated — review "
        "the transcript; run block-sender against this number if it is abuse. "
        "Unreviewed repeats are blocked automatically."
    )


__all__ = ["sweep_bot_activity"]
