"""Recovery sweeps (§20, Phase 14 / D-15).

Some failures are deliberately swallowed rather than raised, because raising would do
more damage than the failure itself. The clearest case is an inbound message: it is
committed first, then its conversation turn is enqueued, and a queue failure at that
point is logged and left — raising would reprocess the whole webhook and re-run its side
effects, including a possible duplicate opt-out acknowledgement.

The cost of that choice is a customer who messaged and will never be answered, visible
only in a log line. This sweep is the other half of the bargain: it finds inbound
messages with no reply and no reply in flight, and enqueues the turn again.

**Idempotent by construction.** It enqueues the same deterministic task name the webhook
would have used, and `process_conversation_turn` checks for an existing reply under the
same derived key before doing anything. A message already in flight is therefore
enqueued at worst once more and answered at most once.

The sweep is deliberately conservative about *when* a message counts as stranded. A
message stored seconds ago is normal, not stranded, and re-enqueueing it would race the
task already on its way.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import String, cast, exists, literal, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.config import get_settings
from app.core.logging import get_logger
from app.domain.enums import ConversationStatus, MessageDirection
from app.integrations.queue import TaskRequest, TaskType
from app.integrations.registry import build_queue_provider
from app.persistence.database import get_session_factory
from app.persistence.models import Conversation, Message
from app.persistence.repositories import rate_limits as rate_limit_repo
from app.worker.handlers.conversation import reply_idempotency_key

logger = get_logger(__name__)

# The prefix `reply_idempotency_key` uses. Declared here so the SQL below and the Python
# there cannot drift apart; a test asserts they agree.
REPLY_KEY_PREFIX = "reply-"


async def sweep_unanswered_messages(task: TaskRequest) -> dict[str, int | str]:
    """Re-enqueue conversation turns that were never queued (D-15).

    `grace_seconds` in the payload overrides the configured grace period. It exists so a
    test can sweep without waiting, and so an operator can run a tighter sweep by hand
    after a known queue outage.
    """
    settings = get_settings()

    raw_grace = task.payload.get("grace_seconds")
    grace = (
        int(raw_grace)
        if isinstance(raw_grace, int) and not isinstance(raw_grace, bool) and raw_grace >= 0
        else settings.sweep_unanswered_grace_seconds
    )

    factory = get_session_factory()
    async with factory() as session:
        stranded = await find_unanswered_messages(
            session, grace_seconds=grace, limit=settings.sweep_batch_size
        )

    if not stranded:
        logger.info("sweep_unanswered_none")
        return {"result": "swept", "found": 0, "requeued": 0}

    queue = build_queue_provider(settings)
    requeued = 0

    for message_id in stranded:
        try:
            await queue.enqueue(
                TaskRequest(
                    task_type=TaskType.PROCESS_CONVERSATION_TURN,
                    # The same deterministic name the webhook would have used, so a task
                    # somehow already queued collapses rather than producing a second
                    # reply (§20).
                    task_id=f"turn-{message_id}",
                    payload={"message_id": str(message_id)},
                )
            )
            requeued += 1
        except Exception as exc:  # noqa: BLE001 - one failure must not end the sweep
            logger.warning("sweep_requeue_failed", message_id=str(message_id), error=str(exc))

    # A warning rather than info: finding anything here means a turn was lost earlier,
    # which is worth noticing even though the sweep has just repaired it.
    logger.warning("sweep_unanswered_requeued", found=len(stranded), requeued=requeued)
    return {"result": "swept", "found": len(stranded), "requeued": requeued}


async def sweep_rate_limit_counters(task: TaskRequest) -> dict[str, int | str]:
    """Delete rate limit counters whose window has passed (§19).

    Housekeeping, not correctness: an expired window is already unreachable because the
    window start is part of the key. Without this the table grows by one row per key per
    window forever.

    Separate from the unanswered sweep so a failure in one does not stop the other, and
    so they can be scheduled at different intervals.
    """
    factory = get_session_factory()
    async with factory() as session:
        removed = await rate_limit_repo.delete_expired(session)
        await session.commit()

    logger.info("sweep_rate_limit_counters", removed=removed)
    return {"result": "swept", "removed": removed}


async def find_unanswered_messages(
    session: AsyncSession, *, grace_seconds: int, limit: int
) -> list[uuid.UUID]:
    """Inbound messages with no reply and no reply in flight.

    A message qualifies when all of these hold:

    - it is inbound and belongs to a conversation;
    - it is older than the grace period, so a turn legitimately in flight is not
      mistaken for a stranded one;
    - no outbound message carries its derived reply key — the same check
      `process_conversation_turn` makes, so the two cannot disagree;
    - its conversation is not CLOSED, because [D36] means a closed conversation is never
      replied into.

    Deliberately *not* filtered here: opt-outs. A `STOP` message never had a turn queued
    by design, so it will match — and the turn handler's own suppression check refuses
    it. One place decides that, and it is the handler.
    """
    cutoff = datetime.now(UTC) - timedelta(seconds=grace_seconds)

    inbound = aliased(Message)
    reply = aliased(Message)

    # 'reply-' || inbound.id — the SQL form of `reply_idempotency_key`.
    expected_key = literal(REPLY_KEY_PREFIX) + cast(inbound.id, String)

    already_answered = exists(
        select(reply.id).where(
            reply.direction == MessageDirection.OUTBOUND,
            reply.idempotency_key == expected_key,
        )
    )

    statement = (
        select(inbound.id)
        .join(Conversation, Conversation.id == inbound.conversation_id)
        .where(
            inbound.direction == MessageDirection.INBOUND,
            inbound.created_at < cutoff,
            Conversation.status != ConversationStatus.CLOSED,
            ~already_answered,
        )
        .order_by(inbound.created_at)
        .limit(limit)
    )

    result = await session.execute(statement)
    return list(result.scalars().all())


def reply_key_for(message_id: uuid.UUID) -> str:
    """The key the turn handler will use, exposed so a test can assert they agree."""
    return reply_idempotency_key(str(message_id))


__all__ = [
    "REPLY_KEY_PREFIX",
    "find_unanswered_messages",
    "reply_key_for",
    "sweep_rate_limit_counters",
    "sweep_unanswered_messages",
]
