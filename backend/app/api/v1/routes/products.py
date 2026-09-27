"""Products and their segments (§28, Phase 26).

What the dashboard's product switcher and segment filters are built from. Products and
segments are rows ([D49]): nothing here knows how many there are or what they are
called, so adding a fourth product is an insert rather than a release.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import CurrentUser, DbSession
from app.api.schemas import ProductDetailResponse, ProductResponse, SegmentResponse
from app.api.scope import PRODUCT_PREFIX, ProductScope
from app.persistence.repositories import products as product_repo

router = APIRouter(tags=["products"])
scoped_router = APIRouter(prefix=PRODUCT_PREFIX, tags=["products"])


@router.get("/products", response_model=list[ProductResponse])
async def list_products(user: CurrentUser, session: DbSession) -> list[ProductResponse]:
    """Every product, active or not.

    All internal users may see all products ([D46]). The switcher offers the active
    ones; an inactive one is still returned so its history stays reachable.
    """
    products = await product_repo.list_products(session)
    return [ProductResponse.model_validate(product) for product in products]


@scoped_router.get("", response_model=ProductDetailResponse)
async def get_product(product: ProductScope, session: DbSession) -> ProductDetailResponse:
    """One product, with its segments and whether its channels are configured.

    Segments are all returned, inactive ones included, because historical rows may carry
    them and a filter that cannot select them would hide data. `has_calendar` and
    `has_whatsapp_number` let the dashboard explain an empty page — "no calendar is
    configured" — instead of leaving a zero to be misread. No identifier or secret is
    exposed by either.
    """
    segments = await product_repo.list_segments(session, product.id)
    return ProductDetailResponse(
        id=product.id,
        slug=product.slug,
        display_name=product.display_name,
        is_active=product.is_active,
        segments=[SegmentResponse.model_validate(segment) for segment in segments],
        has_calendar=bool(product.google_calendar_id),
        has_whatsapp_number=bool(product.whatsapp_phone_number_id),
        has_meeting_url=bool(product.meeting_url),
    )


__all__ = ["router", "scoped_router"]
