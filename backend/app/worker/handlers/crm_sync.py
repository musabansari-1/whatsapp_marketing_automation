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

from app.core.config import CRMProviderName, Settings, get_settings
from app.core.correlation import bind_log_context
from app.core.errors import ProviderError
from app.core.logging import get_logger
from app.domain import crm as crm_domain
from app.domain import mis as mis_domain
from app.domain.enums import BookingStatus
from app.domain.retry import RetryPolicy, backoff_seconds, classify, should_retry
from app.integrations.crm import ContactUpsert
from app.integrations.crm.mis import MISClient
from app.integrations.queue import TaskRequest, TaskType
from app.integrations.registry import build_crm_provider, build_queue_provider
from app.persistence.database import get_session_factory
from app.persistence.models import (
    Audience,
    Booking,
    Campaign,
    CampaignRecipient,
    Conversation,
    Lead,
    Product,
    Segment,
)
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

        if settings.crm_provider is CRMProviderName.MIS:
            # Product-aware (one MIS workspace per product), so the multi-product
            # deferral below does not apply.
            product = await session.get(Product, lead.product_id)
            if product is None:
                raise ValueError(f"Product {lead.product_id} not found")
            mis_args = (
                product.slug,
                await _build_snapshot(session, lead),
                lead.crm_contact_id is not None,
            )
        else:
            mis_args = None

    if mis_args is not None:
        product_slug, mis_snapshot, existed = mis_args
        return await _sync_to_mis(
            settings, str(lead_id), product_slug, mis_snapshot, existed=existed
        )

    async with factory() as session:
        lead = await session.get(Lead, uuid.UUID(str(lead_id)))
        if lead is None:
            raise ValueError(f"Lead {lead_id} not found")

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


# ---------------------------------------------------------------- MIS Tracker


async def _sync_to_mis(
    settings: Settings,
    lead_id: str,
    product_slug: str,
    snapshot: crm_domain.LeadSnapshot,
    *,
    existed: bool,
) -> dict[str, str | bool]:
    """Post one lead to the product's MIS workspace.

    Product-aware, so unlike the flat HubSpot properties it is safe with several
    products: each maps to its own workspace and secret. A product with no mapping is
    skipped, and so is a lead that never engaged and is not in the MIS yet.
    """
    workspace = settings.mis_workspaces.get(product_slug)
    secret = settings.mis_secrets.get(product_slug)
    if not workspace or secret is None or not settings.mis_events_url:
        logger.info("mis_sync_skipped", reason="product_not_mapped", product=product_slug)
        return {"result": "skipped", "lead_id": lead_id, "synced": False}
    if not existed and not mis_domain.is_engaged(snapshot.status):
        logger.info("mis_sync_skipped", reason="not_engaged", status=snapshot.status)
        return {"result": "skipped", "lead_id": lead_id, "synced": False}

    client = MISClient(
        events_url=settings.mis_events_url, timeout_seconds=settings.mis_timeout_seconds
    )
    synced_at = datetime.now(UTC)
    try:
        answer = await client.post_event(
            workspace=workspace,
            secret=secret.get_secret_value(),
            data=mis_domain.build_event(
                snapshot,
                lead_id=lead_id,
                product_slug=product_slug,
                dashboard_url=settings.mis_dashboard_url,
            ),
        )
    except ProviderError as exc:
        await _record_failure(lead_id, str(exc))
        _raise_if_retryable(settings, exc)
        logger.warning("mis_sync_failed", error=str(exc))
        return {"result": "failed", "lead_id": lead_id, "synced": False}

    mis_lead_id = answer.get("lead_id")
    factory = get_session_factory()
    async with factory() as session:
        lead = await session.get(Lead, uuid.UUID(lead_id))
        if lead is not None:
            # Only a lead the MIS actually holds gets an id; one it declined (not engaged,
            # unknown there) stays unsynced so a later engagement still creates it.
            if mis_lead_id:
                lead.crm_contact_id = str(mis_lead_id)[:64]
                lead.crm_synced_at = synced_at
            lead.crm_sync_error = None
            await session.commit()

    logger.info("mis_sync_completed", created=bool(answer.get("created")), held=bool(mis_lead_id))
    return {
        "result": "synced" if mis_lead_id else "skipped",
        "lead_id": lead_id,
        "synced": bool(mis_lead_id),
    }


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

    segment_name = await _segment_name(session, lead)

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
        segment_name=segment_name,
        demo_start=booking.start_time if booking else None,
        demo_status=booking.status.value if booking else None,
        demo_timezone=booking.timezone if booking else None,
    )


async def _segment_name(session: AsyncSession, lead: Lead) -> str | None:
    """The segment a person calling this lead should know about.

    A lead has no segment column ([D48]), so it is read where it is recorded, most
    specific first: the latest conversation (the operative segment for the AI), then the
    latest campaign that messaged the lead, then the audience that introduced it.
    """
    candidates = (
        select(Segment.display_name)
        .join(Conversation, Conversation.segment_id == Segment.id)
        .where(Conversation.lead_id == lead.id)
        .order_by(Conversation.created_at.desc())
        .limit(1),
        select(Segment.display_name)
        .join(Campaign, Campaign.segment_id == Segment.id)
        .join(CampaignRecipient, CampaignRecipient.campaign_id == Campaign.id)
        .where(CampaignRecipient.lead_id == lead.id)
        .order_by(CampaignRecipient.created_at.desc())
        .limit(1),
    )
    for query in candidates:
        name = await session.scalar(query)
        if name:
            return str(name)
    if lead.source_audience_id:
        name = await session.scalar(
            select(Segment.display_name)
            .join(Audience, Audience.segment_id == Segment.id)
            .where(Audience.id == lead.source_audience_id)
        )
        if name:
            return str(name)
    return None


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
