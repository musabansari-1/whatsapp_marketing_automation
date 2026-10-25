"""An approved template another system asked for (`/integrations/.../template-messages`).

The API validated and queued; the worker sends, because the worker is where the
outbound rules live ([D16], §8): never to a suppressed number, always from the number
the product owns ([D40]), at most once per caller key. Suppression is checked again
here because consent can be withdrawn between the request and the send.

A template needs no 24-hour window, so unlike a team reply this can reach someone
who never wrote to the number. It is recorded like any outbound message — the lead
and conversation are created if they do not exist yet — so the team inbox shows it,
and when the person answers, the assistant reads what they are answering. The turn
is marked ``{"sent_by": "system"}``.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

from app.core.config import get_settings
from app.core.correlation import bind_log_context
from app.core.errors import ProviderError
from app.core.logging import get_logger
from app.domain.campaign import META_STATUS_APPROVED
from app.domain.enums import ConversationStatus, MessageStatus
from app.integrations.queue import TaskRequest
from app.integrations.registry import WhatsAppNotConfiguredError, build_whatsapp_provider
from app.integrations.whatsapp import OutboundTemplateMessage
from app.persistence.database import get_session_factory
from app.persistence.models import Product, WhatsAppTemplate
from app.persistence.repositories import conversations as conversation_repo
from app.persistence.repositories import suppressions as suppression_repo
from app.persistence.repositories import webhooks as webhook_repo
from app.worker.handlers.conversation import _raise_if_retryable

logger = get_logger(__name__)

_PLACEHOLDER = re.compile(r"\{\{(\d+)\}\}")


def template_notification_idempotency_key(product_id: str, caller_key: str) -> str:
    """One send per caller message and product, however often the task is redelivered."""
    return f"notify-{uuid.UUID(product_id).hex}-{caller_key}"


def render_template_body(body_preview: str | None, variables: list[str]) -> str:
    """The text the person received, for the transcript.

    `{{n}}` becomes the n-th variable; one the caller did not supply stays as written
    rather than vanishing, so the transcript never claims a message nobody sent.
    """

    def fill(match: re.Match[str]) -> str:
        index = int(match.group(1)) - 1
        return variables[index] if 0 <= index < len(variables) else match.group(0)

    return _PLACEHOLDER.sub(fill, body_preview or "")


async def send_template_notification(task: TaskRequest) -> dict[str, str]:
    settings = get_settings()
    product_id = task.payload.get("product_id")
    template_id = task.payload.get("template_id")
    phone = task.payload.get("phone_e164")
    caller_key = task.payload.get("idempotency_key")
    full_name = task.payload.get("full_name") or None
    variables = [str(v) for v in task.payload.get("variables") or []]
    if not product_id or not template_id or not phone or not caller_key:
        raise ValueError("product_id, template_id, phone_e164 and idempotency_key are required")

    idempotency_key = template_notification_idempotency_key(str(product_id), str(caller_key))
    factory = get_session_factory()

    async with factory() as session:
        product = await session.get(Product, uuid.UUID(str(product_id)))
        template = await session.get(WhatsAppTemplate, uuid.UUID(str(template_id)))
        if product is None or template is None:
            raise ValueError(f"Product {product_id} or template {template_id} not found")

        if await conversation_repo.find_reply_by_idempotency_key(session, idempotency_key):
            return {"result": "already_sent"}
        if await suppression_repo.is_suppressed(session, str(phone)):
            logger.info("template_notification_not_sent", reason="suppressed")
            return {"result": "suppressed"}
        # Approval can be withdrawn between the request and the send (Meta pauses or
        # disables templates); the sync keeps provider_status current.
        if template.product_id != product.id or template.provider_status != META_STATUS_APPROVED:
            logger.info("template_notification_not_sent", reason="template_unusable")
            return {"result": "template_unusable"}

        lead = await webhook_repo.get_or_create_lead(
            session, product_id=product.id, phone_e164=str(phone), full_name=full_name
        )
        conversation = await webhook_repo.get_or_create_conversation(session, lead)
        await session.commit()
        conversation_id = conversation.id
        bind_log_context(conversation_id=str(conversation_id), lead_id=str(lead.id))

        message = OutboundTemplateMessage(
            to_e164=str(phone),
            template_name=template.name,
            language=template.language,
            variables=tuple(variables),
            idempotency_key=idempotency_key,
            header_type=template.header_type,
            header_media_url=template.header_media_url,
        )
        body = render_template_body(template.body_preview, variables)
        template_name = template.name

    try:
        whatsapp = build_whatsapp_provider(settings, product=product)
        result = await whatsapp.send_template_message(message)
    except WhatsAppNotConfiguredError as exc:
        logger.warning(
            "template_notification_not_sent", reason="whatsapp_not_configured", error=str(exc)
        )
        return {"result": "whatsapp_not_configured"}
    except ProviderError as exc:
        _raise_if_retryable(settings, exc)
        logger.warning("template_notification_send_failed", error=str(exc))
        return {"result": "send_failed"}

    async with factory() as session:
        stored = await conversation_repo.get_conversation(session, conversation_id)
        sender = await conversation_repo.get_lead(session, stored.lead_id) if stored else None
        if stored is None or sender is None:
            raise ValueError(f"Conversation {conversation_id} disappeared mid-send")

        sent = await conversation_repo.record_outbound_reply(
            session,
            lead=sender,
            conversation=stored,
            body=body,
            idempotency_key=idempotency_key,
            provider_message_id=result.provider_message_id,
            status=MessageStatus.SENT,
            sent_at=datetime.now(UTC),
            template_name=template_name,
        )
        await conversation_repo.record_turn(
            session,
            conversation=stored,
            role=conversation_repo.ROLE_ASSISTANT,
            content=body,
            message_id=sent.id,
            tool_calls={"sent_by": "system", "template": template_name},
        )
        await conversation_repo.touch_outbound(session, stored)
        if stored.status is ConversationStatus.ACTIVE:
            await conversation_repo.transition_conversation(
                session, stored, ConversationStatus.WAITING_FOR_USER
            )
        await session.commit()

    logger.info("template_notification_sent", template_name=template_name)
    return {"result": "sent"}


__all__ = [
    "render_template_body",
    "send_template_notification",
    "template_notification_idempotency_key",
]
