"""A reply typed by a team member in the dashboard.

The API validates and queues; the worker sends, because the worker is where the
outbound rules live ([D16], §8): a reply goes out only inside the 24-hour window,
never to a suppressed number, never into a closed conversation, and always from the
number the conversation's product owns ([D40]). The API checks the same things first
so the dashboard can say why at once; they are checked again here because consent
can be withdrawn between the click and the send.

The reply is written to the transcript as an assistant turn marked
``{"sent_by": "team"}``, so the model reads what the team said on its next turn and
the dashboard can label it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.core.config import get_settings
from app.core.correlation import bind_log_context
from app.core.errors import ProviderError
from app.core.logging import get_logger
from app.domain.enums import ConversationStatus, MessageStatus
from app.domain.whatsapp_webhook import is_within_service_window
from app.integrations.queue import TaskRequest
from app.integrations.registry import WhatsAppNotConfiguredError, build_whatsapp_provider
from app.integrations.whatsapp import OutboundTextMessage
from app.persistence.database import get_session_factory
from app.persistence.models import Lead, Product
from app.persistence.repositories import conversations as conversation_repo
from app.persistence.repositories import suppressions as suppression_repo
from app.worker.handlers.conversation import _raise_if_retryable

logger = get_logger(__name__)


def team_reply_idempotency_key(request_id: str) -> str:
    """One send per dashboard click, however often the task is redelivered."""
    return f"team-reply-{request_id}"


async def send_team_reply(task: TaskRequest) -> dict[str, str]:
    settings = get_settings()
    conversation_id = task.payload.get("conversation_id")
    request_id = task.payload.get("request_id")
    body = str(task.payload.get("body") or "").strip()
    user_id = task.payload.get("user_id")
    if not conversation_id or not request_id or not body:
        raise ValueError("conversation_id, request_id and body are required")

    idempotency_key = team_reply_idempotency_key(str(request_id))
    factory = get_session_factory()

    async with factory() as session:
        conversation = await conversation_repo.get_conversation(
            session, uuid.UUID(str(conversation_id))
        )
        if conversation is None:
            raise ValueError(f"Conversation {conversation_id} not found")
        lead = await session.get(Lead, conversation.lead_id)
        if lead is None:
            raise ValueError(f"Lead for conversation {conversation_id} not found")
        bind_log_context(conversation_id=str(conversation.id), lead_id=str(lead.id))

        if await conversation_repo.find_reply_by_idempotency_key(session, idempotency_key):
            return {"result": "already_sent"}
        if conversation.status is ConversationStatus.CLOSED:
            logger.info("team_reply_not_sent", reason="closed")
            return {"result": "conversation_closed"}
        if await suppression_repo.is_suppressed(session, lead.phone_e164):
            logger.info("team_reply_not_sent", reason="suppressed")
            return {"result": "suppressed"}
        if not is_within_service_window(
            conversation.last_inbound_at, window_hours=settings.customer_service_window_hours
        ):
            logger.info("team_reply_not_sent", reason="outside_service_window")
            return {"result": "outside_service_window"}

        product = await session.get(Product, conversation.product_id)
        phone = lead.phone_e164

    try:
        if product is None:  # pragma: no cover - conversations.product_id is a NOT NULL FK
            raise WhatsAppNotConfiguredError(f"Product {conversation.product_id} no longer exists")
        whatsapp = build_whatsapp_provider(settings, product=product)
        result = await whatsapp.send_text_message(
            OutboundTextMessage(to_e164=phone, body=body, idempotency_key=idempotency_key)
        )
    except WhatsAppNotConfiguredError as exc:
        logger.warning("team_reply_not_sent", reason="whatsapp_not_configured", error=str(exc))
        return {"result": "whatsapp_not_configured"}
    except ProviderError as exc:
        _raise_if_retryable(settings, exc)
        logger.warning("team_reply_send_failed", error=str(exc))
        return {"result": "send_failed"}

    async with factory() as session:
        conversation = await conversation_repo.get_conversation(
            session, uuid.UUID(str(conversation_id))
        )
        lead = await session.get(Lead, conversation.lead_id) if conversation else None
        if conversation is None or lead is None:
            raise ValueError(f"Conversation {conversation_id} disappeared mid-send")

        sent = await conversation_repo.record_outbound_reply(
            session,
            lead=lead,
            conversation=conversation,
            body=body,
            idempotency_key=idempotency_key,
            provider_message_id=result.provider_message_id,
            status=MessageStatus.SENT,
            sent_at=datetime.now(UTC),
        )
        await conversation_repo.record_turn(
            session,
            conversation=conversation,
            role=conversation_repo.ROLE_ASSISTANT,
            content=body,
            message_id=sent.id,
            tool_calls={"sent_by": "team", "user_id": str(user_id) if user_id else None},
        )
        await conversation_repo.touch_outbound(session, conversation)
        await session.commit()

    logger.info("team_reply_sent")
    return {"result": "sent"}


__all__ = ["send_team_reply", "team_reply_idempotency_key"]
