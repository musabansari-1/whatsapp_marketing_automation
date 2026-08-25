"""WhatsApp webhooks (specification §8, §16, §19, §20).

Meta calls these directly, bypassing the BFF ([D13]), so they carry their own
authentication: a verify token on the subscription handshake and an HMAC signature on
every delivery.

The POST handler does the minimum §8 permits — verify, persist, enqueue, return. All
real processing happens in the worker, because Meta retries aggressively on anything
slow and a webhook that does work inline turns a slow database into a flood of
duplicate deliveries.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response, status
from sqlalchemy.exc import IntegrityError

from app.api.deps import DbSession, Queue, SettingsDep
from app.api.rate_limit import caller_address, enforce
from app.core.logging import get_logger
from app.domain.rate_limit import RateLimitScope
from app.domain.whatsapp_webhook import verify_signature, verify_subscription
from app.integrations.queue import TaskRequest, TaskType
from app.persistence.repositories import webhooks as webhook_repo

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
logger = get_logger(__name__)


@router.get("/whatsapp")
async def verify_whatsapp_webhook(request: Request, settings: SettingsDep) -> Response:
    """Meta's subscription handshake (§8).

    Meta calls this once when the webhook is configured and expects the challenge
    echoed back as plain text.
    """
    if settings.whatsapp_verify_token is None:
        logger.error("whatsapp_webhook_verify_unconfigured")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Webhook verification is not configured",
        )

    challenge = verify_subscription(
        mode=request.query_params.get("hub.mode"),
        token=request.query_params.get("hub.verify_token"),
        challenge=request.query_params.get("hub.challenge"),
        verify_token=settings.whatsapp_verify_token.get_secret_value(),
    )

    if challenge is None:
        logger.warning("whatsapp_webhook_verify_rejected")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Verification failed")

    logger.info("whatsapp_webhook_verified")
    return Response(content=challenge, media_type="text/plain")


@router.post("/whatsapp")
async def receive_whatsapp_webhook(
    request: Request, session: DbSession, settings: SettingsDep, queue: Queue
) -> dict[str, str]:
    """Accept a webhook delivery.

    Always answers 200 once the signature checks out, even for a payload that yields
    nothing actionable. Meta treats any non-2xx as a failure and redelivers, so
    returning an error for a message we simply do not model would produce an endless
    retry loop.
    """
    # Before reading the body (§19). §19's BFF note is explicit that "the Cloud Run API
    # therefore remains publicly reachable for webhooks", so this endpoint is
    # internet-facing: the signature check rejects forged calls cheaply, but nothing
    # else bounds how many arrive. Generous, because Meta legitimately bursts and
    # refusing a real delivery costs a customer message.
    await enforce(
        session,
        settings,
        scope=RateLimitScope.WEBHOOK_IP,
        identifier=caller_address(request),
    )

    raw_body = await request.body()

    if settings.whatsapp_app_secret is None:
        # Refusing is safer than accepting unverified webhooks, which would let
        # anyone fabricate customer replies and opt-outs (§19).
        logger.error("whatsapp_webhook_unconfigured")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Webhook processing is not configured",
        )

    if not verify_signature(
        payload=raw_body,
        signature_header=request.headers.get("X-Hub-Signature-256"),
        app_secret=settings.whatsapp_app_secret.get_secret_value(),
    ):
        logger.warning("whatsapp_webhook_bad_signature")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid signature")

    try:
        payload: dict[str, Any] = json.loads(raw_body)
    except ValueError:
        logger.warning("whatsapp_webhook_unparseable_body")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Body is not valid JSON"
        ) from None

    event_id = webhook_repo.derive_event_id(payload, raw_body)

    try:
        event = await webhook_repo.record(session, provider_event_id=event_id, payload=payload)
    except IntegrityError:
        # The unique constraint on provider_event_id is the §8 duplicate-event
        # protection. Meta redelivers routinely, and a repeat is a success, not an
        # error — reporting failure would only make it redeliver again.
        await session.rollback()
        logger.info("whatsapp_webhook_duplicate", provider_event_id=event_id)
        return {"status": "duplicate"}

    await queue.enqueue(
        TaskRequest(
            task_type=TaskType.PROCESS_INBOUND_MESSAGE,
            # Derived from the event, so a redelivered webhook that somehow got past
            # the unique constraint still collapses at the queue (§20).
            task_id=f"webhook-{event_id}",
            payload={"webhook_event_id": str(event.id)},
        )
    )

    logger.info("whatsapp_webhook_accepted", provider_event_id=event_id)
    return {"status": "accepted"}
