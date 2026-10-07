"""WhatsApp template management (§28, [D6], [D57], Phase 26).

One Meta App and one shared WABA give one template pool, and Meta records no owner on a
template. So the platform holds two kinds of template:

    assigned     owned by one product; usable by that product's campaigns
    unassigned   discovered by the WABA-level sync; usable by no campaign

This is where an operator sees both and turns the second into the first. Assignment is
ADMIN-only, explicit, and never inferred — not from a name, a prefix, the WABA, a phone
number or the product that happens to be selected. An owned template cannot be moved to
another product through here: that would silently change what the first product's
existing campaigns may send, and if it is ever genuinely needed it deserves its own
workflow.

Nothing here creates, edits or approves a template. Approval is Meta's ([D6]).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status

from app.api.deps import AdminUser, CurrentUser, DbSession
from app.api.schemas import (
    ProductTemplateResponse,
    SetTemplateHeaderMediaRequest,
    WhatsAppTemplateResponse,
)
from app.api.scope import PRODUCT_PREFIX, ProductScope
from app.core.logging import get_logger
from app.domain.campaign import (
    TemplateAssignmentError,
    TemplateFacts,
    TemplateMediaMissingError,
    TemplateNotApprovedError,
    TemplateOwnershipError,
    assert_header_media_available,
    assert_template_owned_by,
    assert_template_usable,
)
from app.domain.enums import TemplateCategory
from app.persistence.models import Product, WhatsAppTemplate
from app.persistence.repositories import templates as template_repo

router = APIRouter(prefix=f"{PRODUCT_PREFIX}/whatsapp-templates", tags=["templates"])
global_router = APIRouter(prefix="/whatsapp-templates", tags=["templates"])
logger = get_logger(__name__)


def usable_for_campaigns(template: WhatsAppTemplate, product: Product) -> bool:
    """Whether this product's campaigns could use the template right now.

    The same two rules campaign creation enforces, asked as a question rather than
    raised: Meta approved it as a marketing template ([D6]), and this product owns it
    ([D57]). Computed here so the picker and the refusal cannot disagree.
    """
    try:
        assert_template_usable(
            TemplateFacts(
                provider_status=template.provider_status,
                is_marketing=template.category is TemplateCategory.MARKETING,
            )
        )
        assert_template_owned_by(
            template_product_id=template.product_id, campaign_product_id=product.id
        )
        assert_header_media_available(
            header_type=template.header_type, media_url=template.header_media_url
        )
    except (TemplateNotApprovedError, TemplateOwnershipError, TemplateMediaMissingError):
        return False
    return True


def _with_usability(template: WhatsAppTemplate, product: Product) -> ProductTemplateResponse:
    return ProductTemplateResponse(
        **WhatsAppTemplateResponse.model_validate(template).model_dump(),
        usable_for_campaigns=usable_for_campaigns(template, product),
    )


@router.get("", response_model=list[ProductTemplateResponse])
async def list_product_templates(
    product: ProductScope, session: DbSession
) -> list[ProductTemplateResponse]:
    """Every template this product owns, whatever Meta's current status.

    All of them rather than only the usable ones, so a template Meta paused or rejected
    stays visible with its status instead of silently vanishing from the product.
    `usable_for_campaigns` is what the campaign picker filters on.
    """
    templates = await template_repo.list_owned(session, product_id=product.id)
    return [_with_usability(template, product) for template in templates]


@router.post("/{template_id}/assign", response_model=ProductTemplateResponse)
async def assign_template(
    template_id: uuid.UUID, product: ProductScope, user: AdminUser, session: DbSession
) -> ProductTemplateResponse:
    """Give an unassigned shared-WABA template to this product. ADMIN-only.

    Allowed for an inactive product: assigning templates is how a product is prepared
    before it goes live, and it reaches no customer.

    404 for a template that does not exist. 409 when another product owns it, or when
    this product already owns a template with the same name and language. Repeating an
    assignment this product already holds succeeds and changes nothing.
    """
    if await session.get(WhatsAppTemplate, template_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found")

    try:
        template = await template_repo.assign_to_product(
            session, template_id=template_id, product_id=product.id
        )
    except TemplateAssignmentError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    logger.info(
        "whatsapp_template_assigned",
        template_id=str(template.id),
        product_id=str(product.id),
        by_user_id=str(user.id),
    )
    return _with_usability(template, product)


@router.put("/{template_id}/header-media", response_model=ProductTemplateResponse)
async def set_template_header_media(
    template_id: uuid.UUID,
    payload: SetTemplateHeaderMediaRequest,
    product: ProductScope,
    user: AdminUser,
    session: DbSession,
) -> ProductTemplateResponse:
    """Choose the media this product's template header carries. ADMIN-only.

    A template with an IMAGE, VIDEO or DOCUMENT header cannot be sent by Meta unless
    the send reproduces that header with an asset (132012, a permanent refusal). Meta's
    own record holds only a *sample* image, so the asset has to be supplied here.

    Ownership is required, not assumed: the media belongs to one product's brand, and
    this template is that product's. 404 for a template this product does not own.

    Clearing the media is allowed, and leaves a media-header template unsendable — the
    campaign picker then reports it unusable rather than a broadcast failing on every
    recipient.
    """
    template = await session.get(WhatsAppTemplate, template_id)
    if template is None or template.product_id != product.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found")

    if not payload.media_url:
        logger.info(
            "whatsapp_template_header_media_cleared",
            template_id=str(template.id),
            product_id=str(product.id),
            by_user_id=str(user.id),
        )
    else:
        logger.info(
            "whatsapp_template_header_media_set",
            template_id=str(template.id),
            product_id=str(product.id),
            by_user_id=str(user.id),
            header_type=template.header_type.value,
        )

    updated = await template_repo.set_header_media(
        session, template_id=template.id, media_url=payload.media_url
    )
    return _with_usability(updated, product)


first_reply_router = APIRouter(prefix=f"{PRODUCT_PREFIX}/first-reply-template", tags=["templates"])


@first_reply_router.get("", response_model=ProductTemplateResponse)
async def get_first_reply_template(
    product: ProductScope, session: DbSession
) -> ProductTemplateResponse:
    """The welcome sent once to a first-time sender, or 404 when none is selected.

    404 is the placeholder state, not an error: first-time senders get the normal
    AI reply until a real template is selected with the PUT below.
    """
    if product.first_reply_template_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No first-reply template selected"
        )
    template = await session.get(WhatsAppTemplate, product.first_reply_template_id)
    if template is None or template.product_id != product.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No first-reply template selected"
        )
    return _with_usability(template, product)


@first_reply_router.put("/{template_id}", response_model=ProductTemplateResponse)
async def set_first_reply_template(
    template_id: uuid.UUID, product: ProductScope, user: AdminUser, session: DbSession
) -> ProductTemplateResponse:
    """Select the template sent once to a first-time sender of this product. ADMIN-only.

    Template only — the AI still answers from the second message on. Allowed for an
    inactive product: selecting a welcome is preparation, and it reaches no customer
    until someone messages the product.

    404 for a template this product does not own. 409 when Meta has not approved it
    or a media header has no asset chosen yet. Repeating the current selection
    succeeds and changes nothing; clearing is DELETE on this resource.
    """
    template = await session.get(WhatsAppTemplate, template_id)
    if template is None or template.product_id != product.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found")

    try:
        selected = await template_repo.set_first_reply_template(
            session, product_id=product.id, template_id=template.id
        )
    except (TemplateAssignmentError, TemplateNotApprovedError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    logger.info(
        "whatsapp_first_reply_template_set",
        template_id=str(selected.id),
        product_id=str(product.id),
        by_user_id=str(user.id),
    )
    return _with_usability(selected, product)


@first_reply_router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def clear_first_reply_template(
    product: ProductScope, user: AdminUser, session: DbSession
) -> None:
    """Disable the first-reply welcome. ADMIN-only; first-time senders get the AI reply."""
    await template_repo.clear_first_reply_template(session, product_id=product.id)
    logger.info(
        "whatsapp_first_reply_template_cleared",
        product_id=str(product.id),
        by_user_id=str(user.id),
    )


@global_router.get("/unassigned", response_model=list[WhatsAppTemplateResponse])
async def list_unassigned_templates(
    user: CurrentUser, session: DbSession
) -> list[WhatsAppTemplateResponse]:
    """Shared-WABA templates no product owns yet, whatever their status.

    Global, because an unassigned template belongs to no product — listing it under one
    would imply an ownership nobody has decided. Readable by any internal user; only an
    ADMIN can assign.
    """
    templates = await template_repo.list_unassigned(session)
    return [WhatsAppTemplateResponse.model_validate(template) for template in templates]


__all__ = ["first_reply_router", "global_router", "router", "usable_for_campaigns"]
