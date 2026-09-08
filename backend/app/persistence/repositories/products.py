"""Product and segment queries (§28, Phase 19)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.product import resolve_authoring_product
from app.persistence.models import Product, Segment


async def get_product(session: AsyncSession, product_id: uuid.UUID) -> Product | None:
    return await session.get(Product, product_id)


async def list_products(session: AsyncSession) -> Sequence[Product]:
    result = await session.execute(select(Product).order_by(Product.display_name))
    return result.scalars().all()


async def active_product_ids(session: AsyncSession) -> list[uuid.UUID]:
    """Active products, oldest first.

    Ordered so the result is stable, not so the first one can be picked as a default —
    `resolve_authoring_product` refuses when there is more than one.
    """
    result = await session.execute(
        select(Product.id).where(Product.is_active.is_(True)).order_by(Product.created_at)
    )
    return list(result.scalars().all())


async def resolve_product_for_authoring(
    session: AsyncSession, explicit: uuid.UUID | None
) -> uuid.UUID:
    """The product an internal authoring request belongs to.

    Queries the active products and applies the §28 rule. The count genuinely comes from
    the database: hard-coding "one product exists" would make the compatibility path
    outlive the condition that justifies it.

    Never for inbound customer routing ([D40], [D45]).
    """
    return resolve_authoring_product(
        explicit=explicit,
        active_product_ids=await active_product_ids(session),
    )


async def get_segment(session: AsyncSession, segment_id: uuid.UUID) -> Segment | None:
    return await session.get(Segment, segment_id)


async def list_segments(session: AsyncSession, product_id: uuid.UUID) -> Sequence[Segment]:
    result = await session.execute(
        select(Segment)
        .where(Segment.product_id == product_id)
        .order_by(Segment.display_order, Segment.display_name)
    )
    return result.scalars().all()


__all__ = [
    "active_product_ids",
    "get_product",
    "get_segment",
    "list_products",
    "list_segments",
    "resolve_product_for_authoring",
]
