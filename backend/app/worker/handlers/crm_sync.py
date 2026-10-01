"""CRM synchronization (specification §14, §20, [D11], [D18]).

One lead, pushed to the CRM. Asynchronous and retryable, as §14 requires.

**The rule that shapes this module is what it must not break.** §14 is explicit: a
HubSpot outage must not break WhatsApp, AI, RAG, Calendar, the dashboard or core
PostgreSQL state. So synchronization runs in its own task, and callers enqueue it
without waiting for or caring about the result — `queue_crm_sync` swallows its own
failures for exactly that reason. A CRM that is down costs the platform a stale
contact record and nothing else.

Idempotency comes from identity rather than bookkeeping: the upsert searches by phone
and then email before writing, so a retried task updates the contact it already created
instead of adding a second. The stored `crm_contact_id` is a record and a debugging
aid, not the mechanism.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.correlation import bind_log_context
from app.core.errors import ProviderError
from app.core.logging import get_logger
from app.domain import crm as crm_domain
from app.domain.enums import BookingStatus
from app.domain.retry import RetryPolicy, backoff_seconds, classify, should_retry
from app.integrations.crm import ContactUpsert
from app.integrations.queue import TaskRequest, TaskType
from app.integrations.registry import build_crm_provider, build_queue_provider
from app.persistence.database import get_session_factory
from app.persistence.models import Audience, Booking, Campaign, CampaignRecipient, Lead
from app.persistence.repositories import products as product_repo
from app.worker.handlers.campaign_send import RetryableSendError

logger = get_logger(__name__)


async def queue_crm_sync(settings: Settings, lead_id: uuid.UUID) -> bool:
    """Ask for a lead to be synchronized, and never fail the caller.

    Every failure here is swallowed on purpose. §14 forbids a CRM problem breaking the
    platform, and the callers are the send path, the conversation turn and the booking
    flow — none of which should abandon its own work because a queue call failed.

    When the operator kill-switch is off (`CRM_SYNC_ENABLED=false`), nothing is
    enqueued at all: staging runs multi-product with sync deferred ([D58]), so each
    task would only be refused on arrival. The task's own deferral check stays as
    backstop for anything already queued.

    Returns whether the task was enqueued, for callers that want to log it.
    """
    if not settings.crm_sync_enabled:
        return False
    try:
        await build_queue_provider(settings).enqueue(
            TaskRequest(
                task_type=TaskType.SYNC_CRM_CONTACT,
                # Not deterministic per lead: a lead syncs repeatedly as its state
                # changes, and collapsing those into one task would mean the CRM only
                # ever saw the first version.
                payload={"lead_id": str(lead_id)},
            )
        )
        return True
    except Exception as exc:  # noqa: BLE001 - §14: the CRM must not break anything
        logger.warning("crm_sync_enqueue_failed", lead_id=str(lead_id), error=str(exc))
        return False


async def sync_crm_contact(task: TaskRequest) -> dict[str, str | bool]:
    """Push one lead's current state to the CRM."""
    settings = get_settings()

    lead_id = task.payload.get("lead_id")
    if not lead_id:
        raise ValueError("lead_id missing from task payload")

    factory = get_session_factory()

    async with factory() as session:
        lead = await session.get(Lead, uuid.UUID(str(lead_id)))
        if lead is None:
            # Nothing to retry against.
            raise ValueError(f"Lead {lead_id} not found")

        bind_log_context(lead_id=str(lead.id))

        # Deferred while more than one product exists. The CRM properties are flat, so
        # one product's sync would overwrite another's state on the shared contact; the
        # redesign that fixes that is deferred, and until it lands nothing is written —
        # not to HubSpot, and not the lead's sync fields either.
        product_count = await product_repo.count_products(session)
        if not crm_domain.single_product_sync_is_safe(product_count=product_count):
            logger.info(
                "crm_sync_deferred", reason="more_than_one_product", product_count=product_count
            )
            return {"result": "deferred", "lead_id": str(lead_id), "synced": False}

        snapshot = await _build_snapshot(session, lead)
        contact_id = lead.crm_contact_id

    provider = build_crm_provider(settings)
    synced_at = datetime.now(UTC)

    try:
        contact = await provider.upsert_contact(
            ContactUpsert(
                phone_e164=snapshot.phone_e164,
                email=snapshot.email,
                properties=crm_domain.build_properties(snapshot, synced_at=synced_at),
                idempotency_key=f"lead-{lead_id}",
            )
        )
    except ProviderError as exc:
        await _record_failure(str(lead_id), str(exc))
        _raise_if_retryable(settings, exc)
        # Permanent: recorded on the lead and left there. Raising would retry a request
        # that cannot succeed, and §14 means the platform carries on regardless.
        logger.warning("crm_sync_failed", error=str(exc))
        return {"result": "failed", "lead_id": str(lead_id), "synced": False}

    async with factory() as session:
        lead = await session.get(Lead, uuid.UUID(str(lead_id)))
        if lead is not None:
            lead.crm_contact_id = contact.provider_contact_id
            lead.crm_synced_at = synced_at
            lead.crm_sync_error = None
            await session.commit()

    logger.info(
        "crm_sync_completed",
        contact_id=contact.provider_contact_id,
        created=contact_id != contact.provider_contact_id,
    )
    return {"result": "synced", "lead_id": str(lead_id), "synced": True}


# ---------------------------------------------------------------- internals


async def _build_snapshot(session: AsyncSession, lead: Lead) -> crm_domain.LeadSnapshot:
    """Gather what the CRM is told about this lead (§14).

    Read here rather than in the domain mapping so the mapping stays free of
    persistence, and read at sync time rather than passed in the task payload so a
    delayed or retried task sends current state instead of stale state.
    """
    audience_name: str | None = None
    if lead.source_audience_id:
        audience_name = await session.scalar(
            select(Audience.name).where(Audience.id == lead.source_audience_id)
        )

    # The most recent campaign that actually messaged this lead. §14 asks for campaign
    # information "where applicable", and the latest one is what a salesperson opening
    # the contact wants to know.
    campaign_name = await session.scalar(
        select(Campaign.name)
        .join(CampaignRecipient, CampaignRecipient.campaign_id == Campaign.id)
        .where(CampaignRecipient.lead_id == lead.id)
        .order_by(CampaignRecipient.created_at.desc())
        .limit(1)
    )

    # Confirmed bookings only: a cancelled attempt in the CRM would have a salesperson
    # turning up to a demo nobody is attending.
    booking = (
        (
            await session.execute(
                select(Booking)
                .where(Booking.lead_id == lead.id, Booking.status == BookingStatus.CONFIRMED)
                .order_by(Booking.start_time.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )

    return crm_domain.LeadSnapshot(
        phone_e164=lead.phone_e164,
        full_name=lead.full_name,
        email=lead.email,
        company=lead.company,
        designation=lead.designation,
        city=lead.city,
        status=lead.status.value,
        qualification_data=dict(lead.qualification_data or {}),
        opted_out=lead.opted_out,
        source_audience_name=audience_name,
        last_campaign_name=campaign_name,
        demo_start=booking.start_time if booking else None,
        demo_status=booking.status.value if booking else None,
        demo_timezone=booking.timezone if booking else None,
    )


async def _record_failure(lead_id: str, reason: str) -> None:
    """Keep the failure on the lead.

    §14 forbids raising this at the caller, so without a record a CRM that has been
    failing for a week would be invisible until someone opened HubSpot.
    """
    factory = get_session_factory()
    async with factory() as session:
        lead = await session.get(Lead, uuid.UUID(lead_id))
        if lead is None:
            return
        lead.crm_sync_error = reason[:1000]
        await session.commit()


def _raise_if_retryable(settings: Settings, exc: ProviderError) -> None:
    policy = RetryPolicy(
        max_attempts=settings.send_max_attempts,
        base_seconds=settings.send_backoff_base_seconds,
        max_seconds=settings.send_backoff_max_seconds,
        jitter=settings.send_backoff_jitter,
    )
    failure = classify(exc)
    if should_retry(failure=failure, attempts_made=1, policy=policy):
        raise RetryableSendError(
            str(exc), retry_after_seconds=backoff_seconds(attempts_made=1, policy=policy)
        ) from exc


__all__ = ["queue_crm_sync", "sync_crm_contact"]
