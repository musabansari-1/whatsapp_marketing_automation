"""Templates sent on behalf of another system.

A product's own backend — Life Herba's, telling a doctor their profile is approved —
asks the platform to send one of that product's approved templates to one number. The
platform sends it rather than the caller talking to Meta directly, so the message goes
from the product's number, appears in the team inbox, and obeys the same opt-out list
as everything else the platform sends.

Machine to machine: a per-product bearer token (`INTEGRATION_TOKENS`), not a dashboard
session. A token reaches only the product it is configured for, and every failure to
authenticate is the same 401, so a caller cannot probe which slugs exist.

The API validates and queues; the worker sends (`SEND_TEMPLATE_NOTIFICATION`).
"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Header, HTTPException, status

from app.api.deps import DbSession, Queue, SettingsDep
from app.api.schemas import TemplateMessageAccepted, TemplateMessageRequest
from app.core.config import Settings
from app.core.logging import get_logger
from app.domain.campaign import (
    META_STATUS_APPROVED,
    TemplateMediaMissingError,
    assert_header_media_available,
)
from app.domain.phone import normalize_phone
from app.integrations.queue import TaskRequest, TaskType
from app.persistence.models import Product
from app.persistence.repositories import products as product_repo
from app.persistence.repositories import suppressions as suppression_repo
from app.persistence.repositories import templates as template_repo

logger = get_logger(__name__)

router = APIRouter(prefix="/integrations", tags=["integrations"])

_UNAUTHENTICATED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated"
)


def _token_matches(settings: Settings, product_slug: str, authorization: str | None) -> bool:
    expected = settings.integration_tokens.get(product_slug)
    if expected is None or not authorization:
        return False
    scheme, _, presented = authorization.partition(" ")
    if scheme.lower() != "bearer" or not presented:
        return False
    return hmac.compare_digest(presented.encode(), expected.get_secret_value().encode())


async def _authorised_product(
    session: DbSession, settings: Settings, product_slug: str, authorization: str | None
) -> Product:
    if not _token_matches(settings, product_slug, authorization):
        raise _UNAUTHENTICATED
    product = await product_repo.get_by_slug(session, product_slug)
    if product is None:
        # A token configured for a slug that does not exist: the same answer as a bad one.
        raise _UNAUTHENTICATED
    return product


def template_notification_task_id(product: Product, idempotency_key: str) -> str:
    """One task per caller message, so a retried request queues nothing new."""
    return f"template-notification-{product.id.hex}-{idempotency_key}"


@router.post(
    "/{product_slug}/template-messages",
    response_model=TemplateMessageAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def send_template_message(
    product_slug: str,
    payload: TemplateMessageRequest,
    session: DbSession,
    settings: SettingsDep,
    queue: Queue,
    authorization: str | None = Header(default=None),
) -> TemplateMessageAccepted:
    """Queue one approved template to one number.

    422 when the request can never succeed (bad number, a template this product does not
    have approved, the wrong number of variables); 409 when it cannot succeed now (the
    product is inactive, or the person opted out). Callers should not retry either.
    """
    product = await _authorised_product(session, settings, product_slug, authorization)
    if not product.is_active:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Product is inactive")

    phone = normalize_phone(payload.phone, default_region=settings.audience_phone_default_region)
    if phone.e164 is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=f"phone: {phone.error}"
        )

    template = await template_repo.get_owned_by_name(
        session, product_id=product.id, name=payload.template_name, language=payload.language
    )
    if template is None or template.provider_status != META_STATUS_APPROVED:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"Template {payload.template_name} ({payload.language}) is not approved "
                "and assigned to this product"
            ),
        )
    if template.variable_count != len(payload.variables):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"Template {template.name} takes {template.variable_count} variables, "
                f"got {len(payload.variables)}"
            ),
        )
    try:
        assert_header_media_available(
            header_type=template.header_type, media_url=template.header_media_url
        )
    except TemplateMediaMissingError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    if await suppression_repo.is_suppressed(session, phone.e164):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This number has opted out, so it cannot be messaged",
        )

    await queue.enqueue(
        TaskRequest(
            task_type=TaskType.SEND_TEMPLATE_NOTIFICATION,
            task_id=template_notification_task_id(product, payload.idempotency_key),
            payload={
                "product_id": str(product.id),
                "template_id": str(template.id),
                "phone_e164": phone.e164,
                "full_name": payload.full_name,
                "variables": payload.variables,
                "idempotency_key": payload.idempotency_key,
            },
        )
    )
    logger.info(
        "template_notification_queued",
        product_id=str(product.id),
        template_name=template.name,
    )
    return TemplateMessageAccepted(queued=True)
