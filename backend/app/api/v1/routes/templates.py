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
from app.api.schemas import ProductTemplateResponse, WhatsAppTemplateResponse
from app.api.scope import PRODUCT_PREFIX, ProductScope
from app.core.logging import get_logger
from app.domain.campaign import (
    TemplateAssignmentError,
    TemplateFacts,
    TemplateNotApprovedError,
    TemplateOwnershipError,
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
    except (TemplateNotApprovedError, TemplateOwnershipError):
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


__all__ = ["global_router", "router", "usable_for_campaigns"]
