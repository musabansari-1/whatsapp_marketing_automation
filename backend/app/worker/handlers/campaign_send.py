"""Campaign send handler (specification §7, §18, §20, [D16], [D27], [D29]).

This is what finally delivers a broadcast. Everything before Phase 7 prepared work;
this consumes it.

The ordering below is deliberate and each step exists to prevent a specific failure:

1. lock the recipient row — two workers can receive the same task
2. settled already? — a redelivery must not send twice
3. campaign cancelled or paused? — [D29] requires the re-check here, because the task
   was queued before the cancellation existed
4. suppressed? — [D16], because the customer may have opted out since preparation
5. record the attempt *before* sending — a crash mid-send must not look like a
   never-attempted recipient
6. send
7. record the outcome

`RetryableSendError` is raised when the caller should retry; the HTTP layer turns it
into a status Cloud Tasks understands.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.correlation import bind_log_context
from app.core.logging import get_logger
from app.domain.campaign import (
    TemplateMediaMissingError,
    assert_header_media_available,
    is_unsent,
)
from app.domain.enums import (
    CampaignStatus,
    MessageDirection,
    MessageStatus,
    RecipientStatus,
)
from app.domain.product import ProductInactiveError, assert_accepts_new_activity
from app.domain.retry import (
    FailureClass,
    RetryPolicy,
    backoff_seconds,
    classify,
    should_retry,
)
from app.integrations.queue import TaskRequest
from app.integrations.registry import WhatsAppNotConfiguredError, build_whatsapp_provider
from app.integrations.whatsapp import OutboundTemplateMessage
from app.persistence.database import get_session_factory
from app.persistence.models import (
    Campaign,
    CampaignRecipient,
    Lead,
    Message,
    Product,
    WhatsAppSuppression,
    WhatsAppTemplate,
)

logger = get_logger(__name__)


# Long enough for the send route to finish enqueueing a large audience and commit, short
# enough that the queue's attempt limit is not spent waiting.
_NOT_STARTED_RETRY_SECONDS = 30.0


class RetryableSendError(Exception):
    """The send failed but is worth another attempt.

    Carries the computed backoff so the transport can honour it.
    """

    def __init__(self, message: str, *, retry_after_seconds: float) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True, slots=True)
class SendOutcome:
    """What happened, for logging and for the task response."""

    result: str
    recipient_id: str
    provider_message_id: str | None = None


def policy_from(settings: Settings) -> RetryPolicy:
    return RetryPolicy(
        max_attempts=settings.send_max_attempts,
        base_seconds=settings.send_backoff_base_seconds,
        max_seconds=settings.send_backoff_max_seconds,
        jitter=settings.send_backoff_jitter,
    )


async def send_campaign_message(task: TaskRequest) -> dict[str, str | None]:
    """Deliver one campaign message.

    Idempotent by construction: the recipient row is locked, its status is checked
    before anything is sent, and the outbound `messages` row carries a unique
    idempotency key derived from the recipient.
    """
    settings = get_settings()
    recipient_id = task.payload.get("recipient_id")
    if not recipient_id:
        raise ValueError("recipient_id missing from task payload")

    factory = get_session_factory()
    async with factory() as session:
        try:
            outcome = await _process(session, settings, uuid.UUID(str(recipient_id)))
            await session.commit()
        except RetryableSendError:
            # The attempt counter and any status change are worth keeping even though
            # the send failed, so the next attempt sees an accurate count.
            await session.commit()
            raise
        except Exception:
            await session.rollback()
            raise

    logger.info(
        "campaign_send_finished",
        result=outcome.result,
        recipient_id=outcome.recipient_id,
        provider_message_id=outcome.provider_message_id,
    )
    return {
        "result": outcome.result,
        "recipient_id": outcome.recipient_id,
        "provider_message_id": outcome.provider_message_id,
    }


async def _process(
    session: AsyncSession, settings: Settings, recipient_id: uuid.UUID
) -> SendOutcome:
    # FOR UPDATE serialises concurrent workers holding the same task. Without it, two
    # workers could both read PENDING and both send.
    recipient = (
        await session.execute(
            select(CampaignRecipient).where(CampaignRecipient.id == recipient_id).with_for_update()
        )
    ).scalar_one_or_none()

    if recipient is None:
        # Nothing to retry: the recipient is gone.
        raise ValueError(f"Campaign recipient {recipient_id} not found")

    bind_log_context(
        campaign_id=str(recipient.campaign_id),
        lead_id=str(recipient.lead_id),
        recipient_id=str(recipient.id),
    )

    if not is_unsent(recipient.status):
        # Already SENT, DELIVERED, READ, FAILED or SKIPPED. A redelivered task must
        # not produce a second message (§20).
        return SendOutcome("already_settled", str(recipient.id), recipient.provider_message_id)

    campaign = await session.get(Campaign, recipient.campaign_id)
    if campaign is None:
        raise ValueError(f"Campaign {recipient.campaign_id} not found")

    # [D29]: the task may have been queued before the campaign was cancelled.
    if campaign.status is CampaignStatus.CANCELLED:
        recipient.status = RecipientStatus.SKIPPED
        await session.flush()
        logger.info("campaign_send_skipped", reason="campaign_cancelled")
        return SendOutcome("skipped_campaign_cancelled", str(recipient.id))

    if campaign.status is CampaignStatus.PAUSED:
        # Left PENDING rather than skipped: a paused campaign is expected to resume,
        # and this recipient should be sent then.
        logger.info("campaign_send_deferred", reason="campaign_paused")
        return SendOutcome("deferred_campaign_paused", str(recipient.id))

    if campaign.status in (CampaignStatus.READY, CampaignStatus.SCHEDULED):
        # The broadcast has not committed yet. The send route enqueues every task before
        # it commits PROCESSING, and a paced queue delivers the first tasks inside that
        # window. Completing the task here would orphan the recipient: the route then
        # marks it QUEUED and nothing is left in the queue to send it. On 26 October
        # 2026 that left 29 of a 191-recipient broadcast unsent. Ask the queue to come
        # back once the commit has landed.
        logger.info("campaign_send_deferred", reason="campaign_not_started")
        raise RetryableSendError(
            "Campaign has not started yet", retry_after_seconds=_NOT_STARTED_RETRY_SECONDS
        )

    if campaign.status is not CampaignStatus.PROCESSING:
        logger.info("campaign_send_skipped", reason=f"campaign_{campaign.status.value.lower()}")
        return SendOutcome(f"skipped_campaign_{campaign.status.value.lower()}", str(recipient.id))

    # A product deactivated after the broadcast was confirmed sends nothing further.
    # Skipped rather than left pending, so the campaign settles, and a later reactivation
    # cannot quietly resume a broadcast nobody re-confirmed.
    product = await session.get(Product, campaign.product_id)
    if product is None:
        raise ValueError(f"Campaign {campaign.id} has no product")
    try:
        assert_accepts_new_activity(is_active=product.is_active, display_name=product.display_name)
    except ProductInactiveError:
        recipient.status = RecipientStatus.SKIPPED
        await session.flush()
        logger.info("campaign_send_skipped", reason="product_inactive")
        await _complete_campaign_if_finished(session, campaign)
        return SendOutcome("skipped_product_inactive", str(recipient.id))

    # [D16]: the customer may have opted out since the snapshot, or even since the
    # broadcast was confirmed.
    suppressed = (
        await session.execute(
            select(exists().where(WhatsAppSuppression.phone_e164 == recipient.phone_e164))
        )
    ).scalar()
    if suppressed:
        recipient.status = RecipientStatus.SKIPPED
        await session.flush()
        logger.info("campaign_send_skipped", reason="suppressed")
        return SendOutcome("skipped_suppressed", str(recipient.id))

    template = await session.get(WhatsAppTemplate, campaign.template_id)
    if template is None:
        raise ValueError(f"Template {campaign.template_id} not found")

    # A media header with no asset is refused here, by name, rather than sent without
    # its header and refused by Meta (132012) once per recipient. The failure is
    # identical and permanent either way; catching it before the send means the
    # operator learns which template is missing its media from the campaign, and the
    # recipients are not each charged a doomed attempt against the number's quota.
    try:
        assert_header_media_available(
            header_type=template.header_type, media_url=template.header_media_url
        )
    except TemplateMediaMissingError as exc:
        recipient.status = RecipientStatus.FAILED
        recipient.error_code = "template_media_missing"
        recipient.error_message = str(exc)[:1000]
        recipient.failed_at = datetime.now(UTC)
        await session.flush()
        logger.warning(
            "campaign_send_failed",
            reason="template_media_missing",
            header_type=template.header_type.value,
            error=str(exc),
        )
        await _complete_campaign_if_finished(session, campaign)
        return SendOutcome("failed_template_media_missing", str(recipient.id))

    idempotency_key = f"send-{recipient.id}"

    # If a previous attempt sent successfully but crashed before recording, the
    # message row is the evidence. Recovering it avoids a duplicate send.
    existing = (
        await session.execute(select(Message).where(Message.idempotency_key == idempotency_key))
    ).scalar_one_or_none()
    if existing is not None and existing.provider_message_id:
        _mark_sent(recipient, existing.provider_message_id)
        await session.flush()
        return SendOutcome("recovered_prior_send", str(recipient.id), existing.provider_message_id)

    # The campaign's product selects the provider, and therefore the number this goes
    # out from ([D40]). A SustainX campaign cannot be sent through Academicae's number,
    # because no caller supplies a number — the domain object does.
    try:
        provider = build_whatsapp_provider(settings, product=product)
    except WhatsAppNotConfiguredError as exc:
        # The product has no number, and no other number may stand in (Phase 27). Failed
        # rather than retried: another attempt cannot fix configuration.
        recipient.status = RecipientStatus.FAILED
        recipient.error_message = str(exc)[:1000]
        recipient.failed_at = datetime.now(UTC)
        await session.flush()
        logger.warning("campaign_send_failed", reason="whatsapp_not_configured", error=str(exc))
        await _complete_campaign_if_finished(session, campaign)
        return SendOutcome("failed_whatsapp_not_configured", str(recipient.id))

    lead = await session.get(Lead, recipient.lead_id)

    # Counted before the attempt, so a crash mid-send still consumes an attempt rather
    # than allowing unbounded retries.
    recipient.attempts += 1
    attempts_made = recipient.attempts
    await session.flush()

    try:
        sent = await provider.send_template_message(
            OutboundTemplateMessage(
                to_e164=recipient.phone_e164,
                template_name=template.name,
                language=template.language,
                variables=_variables_for(template, lead),
                idempotency_key=idempotency_key,
                # The send mirrors the approved template's shape. Omitting these is
                # what produced Meta's "header: Format mismatch, expected IMAGE,
                # received UNKNOWN" and a permanent failure for every recipient.
                header_type=template.header_type,
                header_media_url=template.header_media_url,
            )
        )
    except Exception as exc:
        # `_handle_failure` raises `RetryableSendError` when another attempt is due, so
        # reaching the lines below means this recipient has genuinely settled as FAILED
        # — and it may have been the campaign's last outstanding one. Without this the
        # campaign stays PROCESSING forever whenever its final recipient fails (D-03).
        outcome = _handle_failure(session, settings, recipient, exc, attempts_made=attempts_made)
        await session.flush()
        await _complete_campaign_if_finished(session, campaign)
        return outcome

    session.add(
        Message(
            lead_id=recipient.lead_id,
            campaign_id=recipient.campaign_id,
            # From the campaign, its authoritative parent (§28.6).
            product_id=campaign.product_id,
            direction=MessageDirection.OUTBOUND,
            status=MessageStatus.SENT,
            provider_message_id=sent.provider_message_id,
            phone_e164=recipient.phone_e164,
            template_name=template.name,
            attempts=attempts_made,
            idempotency_key=idempotency_key,
        )
    )
    _mark_sent(recipient, sent.provider_message_id)
    await session.flush()
    await _complete_campaign_if_finished(session, campaign)

    return SendOutcome("sent", str(recipient.id), sent.provider_message_id)


def _handle_failure(
    session: AsyncSession,
    settings: Settings,
    recipient: CampaignRecipient,
    error: Exception,
    *,
    attempts_made: int,
) -> SendOutcome:
    """Apply the [D27] retry decision to a failed send."""
    policy = policy_from(settings)
    failure = classify(error)
    code = getattr(error, "provider", None)

    if should_retry(failure=failure, attempts_made=attempts_made, policy=policy):
        delay = backoff_seconds(attempts_made=attempts_made, policy=policy)
        logger.warning(
            "campaign_send_retrying",
            failure_class=failure.value,
            attempts_made=attempts_made,
            max_attempts=policy.max_attempts,
            retry_after_seconds=round(delay, 2),
            error=str(error),
        )
        # Left QUEUED, not FAILED: the recipient still has attempts remaining.
        raise RetryableSendError(str(error), retry_after_seconds=delay)

    recipient.status = RecipientStatus.FAILED
    recipient.error_code = str(code) if code else None
    recipient.error_message = str(error)[:1000]
    recipient.failed_at = datetime.now(UTC)

    logger.warning(
        "campaign_send_failed",
        failure_class=failure.value,
        attempts_made=attempts_made,
        exhausted=failure is not FailureClass.PERMANENT,
        error=str(error),
    )
    return SendOutcome(
        "failed_permanent" if failure is FailureClass.PERMANENT else "failed_exhausted",
        str(recipient.id),
    )


def _mark_sent(recipient: CampaignRecipient, provider_message_id: str) -> None:
    recipient.status = RecipientStatus.SENT
    recipient.provider_message_id = provider_message_id
    recipient.sent_at = recipient.sent_at or datetime.now(UTC)


def _variables_for(template: WhatsAppTemplate, lead: Lead | None) -> tuple[str, ...]:
    """Fill the template's positional variables.

    Only the first is populated, with the contact's name, because that is the one
    convention the platform can rely on without knowing the template's text — and
    [D6]/[D23] mean the text belongs to Meta and the client, not to this code.
    Remaining variables are sent empty rather than guessed.
    """
    if template.variable_count <= 0:
        return ()

    name = (lead.full_name if lead else None) or "there"
    return (name, *("" for _ in range(template.variable_count - 1)))


async def _complete_campaign_if_finished(session: AsyncSession, campaign: Campaign) -> None:
    """Move the campaign to COMPLETED once no recipient has work left.

    COMPLETED covers partial success: a campaign where some recipients failed is
    still finished, and `sent_count`/`failed_count` carry the breakdown. There is no
    PARTIAL state, by decision.

    Called from both the success and the failure path. Only the success path used to
    call it, so a campaign whose last recipient failed never left PROCESSING — the
    recipient states stayed correct and the campaign's own state did not.
    """
    outstanding = (
        await session.execute(
            select(
                exists().where(
                    CampaignRecipient.campaign_id == campaign.id,
                    CampaignRecipient.status.in_([RecipientStatus.PENDING, RecipientStatus.QUEUED]),
                )
            )
        )
    ).scalar()

    if outstanding:
        return

    from app.persistence.repositories import campaigns as campaign_repo

    if campaign.status is CampaignStatus.PROCESSING:
        campaign.status = CampaignStatus.COMPLETED
        campaign.completed_at = datetime.now(UTC)

    await campaign_repo.refresh_counts(session, campaign)
    logger.info("campaign_completed", campaign_id=str(campaign.id))
