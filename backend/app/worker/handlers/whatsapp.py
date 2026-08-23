"""WhatsApp task handlers (specification §8, §18, [D5], [D16]).

Where webhook payloads are actually processed. The webhook itself only persists and
enqueues, because §8 requires it to return quickly.

Idempotent throughout (§20): Cloud Tasks redelivers, and every write here either
checks for an existing row or relies on a unique constraint.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.core.config import Settings, get_settings
from app.core.correlation import bind_log_context
from app.core.logging import get_logger
from app.domain.whatsapp_webhook import matches_opt_out, parse_webhook
from app.integrations.queue import TaskRequest, TaskType
from app.integrations.registry import build_queue_provider, build_whatsapp_provider
from app.integrations.whatsapp import OutboundTextMessage
from app.persistence.database import get_session_factory
from app.persistence.repositories import templates as template_repo
from app.persistence.repositories import webhooks as webhook_repo
from app.worker.handlers.crm_sync import queue_crm_sync

logger = get_logger(__name__)


async def process_inbound_webhook(task: TaskRequest) -> dict[str, int]:
    """Process one stored webhook delivery.

    Handles both halves of §8: customer messages and delivery/read/failure statuses,
    which Meta batches into the same POST.
    """
    settings = get_settings()
    event_id = task.payload.get("webhook_event_id")
    if not event_id:
        raise ValueError("webhook_event_id missing from task payload")

    factory = get_session_factory()
    async with factory() as session:
        event = await webhook_repo.get_event(session, uuid.UUID(str(event_id)))
        if event is None:
            raise ValueError(f"Webhook event {event_id} not found")

        bind_log_context(webhook_event_id=str(event.id))

        try:
            parsed = parse_webhook(event.payload)
            statuses = await _apply_statuses(session, parsed.statuses)
            inbound, opt_outs, needs_reply = await _apply_inbound(session, settings, parsed.inbound)

            await webhook_repo.mark_processed(session, event)
            await session.commit()
        except Exception as exc:
            await session.rollback()
            # Recorded on the event so a persistent failure is visible in the
            # dashboard rather than only in logs (§21).
            async with factory() as error_session:
                failed = await webhook_repo.get_event(error_session, uuid.UUID(str(event_id)))
                if failed is not None:
                    await webhook_repo.mark_failed(error_session, failed, str(exc))
                    await error_session.commit()
            raise

    # After the commit, so the task cannot arrive before the row it names. Failing to
    # enqueue must not undo the storage: the message is safely recorded either way, and
    # raising here would reprocess the whole webhook.
    queued = await _queue_conversation_turns(settings, needs_reply)

    logger.info(
        "whatsapp_webhook_processed",
        inbound_messages=inbound,
        status_updates=statuses,
        opt_outs=opt_outs,
        conversation_turns_queued=queued,
    )
    return {
        "inbound": inbound,
        "statuses": statuses,
        "opt_outs": opt_outs,
        "turns_queued": queued,
    }


async def _queue_conversation_turns(settings: Settings, message_ids: list[str]) -> int:
    """Hand each stored customer message to the conversation handler (§9, §18)."""
    if not message_ids:
        return 0

    queue = build_queue_provider(settings)
    queued = 0

    for message_id in message_ids:
        try:
            await queue.enqueue(
                TaskRequest(
                    task_type=TaskType.PROCESS_CONVERSATION_TURN,
                    # Deterministic, so a redelivered webhook collapses into the same
                    # task rather than producing a second reply (§20).
                    task_id=f"turn-{message_id}",
                    payload={"message_id": message_id},
                )
            )
            queued += 1
        except Exception as exc:  # noqa: BLE001 - queueing is not worth losing the message over
            logger.warning(
                "conversation_turn_enqueue_failed", message_id=message_id, error=str(exc)
            )

    return queued


async def _apply_statuses(session: Any, statuses: list[Any]) -> int:
    applied = 0
    for status_event in statuses:
        matched = await webhook_repo.apply_status(
            session,
            provider_message_id=status_event.provider_message_id,
            new_status=status_event.status,
            occurred_at=status_event.timestamp,
            error_code=status_event.error_code,
            error_message=status_event.error_message,
        )
        if matched:
            applied += 1
    return applied


async def _apply_inbound(
    session: Any, settings: Settings, messages: list[Any]
) -> tuple[int, int, list[str]]:
    """Store customer messages, act on opt-outs, and report which need a reply.

    The IDs are returned rather than enqueued here because the rows are not committed
    yet: a task dispatched now could reach a worker before the message it names
    exists.
    """
    stored = 0
    opt_outs = 0
    needs_reply: list[str] = []

    for event in messages:
        lead = await webhook_repo.get_or_create_lead(
            session, phone_e164=event.from_e164, full_name=event.profile_name
        )
        conversation = await webhook_repo.get_or_create_conversation(session, lead)

        message = await webhook_repo.record_inbound_message(
            session,
            lead=lead,
            conversation=conversation,
            provider_message_id=event.provider_message_id,
            body=event.text,
            received_at=event.timestamp,
        )
        if message is None:
            # Already stored: a redelivery. Skip the side effects too, so a repeated
            # STOP does not re-run the opt-out flow.
            continue
        stored += 1

        if matches_opt_out(event.text, settings.opt_out_keywords):
            if await _handle_opt_out(session, settings, lead, event.provider_message_id):
                opt_outs += 1
            # An opt-out gets the acknowledgement above and nothing else. Handing it
            # to the AI would reply to someone who just asked us to stop.
            continue

        needs_reply.append(str(message.id))

    return stored, opt_outs, needs_reply


async def _handle_opt_out(
    session: Any, settings: Settings, lead: Any, source_message_id: str
) -> bool:
    """Suppress a number and stop anything already queued for it ([D5], [D16])."""
    newly_suppressed = await webhook_repo.suppress(
        session, lead=lead, source_message_id=source_message_id
    )
    if not newly_suppressed:
        # Already opted out; acknowledging again would be noise to the customer.
        return False

    cancelled = await webhook_repo.cancel_pending_sends(session, lead.phone_e164)

    # Consent is the field where a stale CRM value does real harm: a salesperson
    # reading "not opted out" may contact someone who asked us to stop.
    await queue_crm_sync(settings, lead.id)
    logger.info(
        "whatsapp_opt_out",
        lead_id=str(lead.id),
        cancelled_pending_sends=cancelled,
    )

    # The acknowledgement is a reply to a message the customer just sent, so it is
    # inside the 24-hour window by definition and needs no template.
    try:
        provider = build_whatsapp_provider(settings)
        await provider.send_text_message(
            OutboundTextMessage(
                to_e164=lead.phone_e164,
                body=settings.msg_opt_out_response,
                idempotency_key=f"optout-ack-{source_message_id}",
            )
        )
    except Exception as exc:  # noqa: BLE001 - acknowledgement is best-effort
        # The suppression already succeeded and is what actually protects the
        # customer. Failing the task here would retry the whole webhook and risk
        # duplicate acknowledgements for no benefit.
        logger.warning("whatsapp_opt_out_ack_failed", error=str(exc))

    return True


async def sync_whatsapp_templates(task: TaskRequest) -> dict[str, int]:
    """Synchronize template metadata from Meta ([D6]).

    Stores what Meta reports, including the status. Nothing here decides approval;
    campaigns filter on the stored value.
    """
    settings = get_settings()
    provider = build_whatsapp_provider(settings)

    templates = await provider.fetch_approved_templates()

    factory = get_session_factory()
    async with factory() as session:
        synced = await template_repo.upsert_many(session, templates)
        await session.commit()

    logger.info("whatsapp_templates_synced", count=synced)
    return {"synced": synced}
