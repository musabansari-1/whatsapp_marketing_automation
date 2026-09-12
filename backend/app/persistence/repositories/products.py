"""Product and segment queries (§28).

Products are rows ([D49]). Nothing here knows how many exist or what any is called, and
there is no default product: every lookup is by an identifier the caller already holds —
an ID, a route slug, or the WhatsApp number a message arrived on.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models import Product, Segment


async def get_product(session: AsyncSession, product_id: uuid.UUID) -> Product | None:
    return await session.get(Product, product_id)


async def get_by_slug(session: AsyncSession, slug: str) -> Product | None:
    """The product a dashboard route names.

    The slug is a permanent route identifier (Phase 26). It locates a product; it never
    decides behaviour.
    """
    result = await session.execute(select(Product).where(Product.slug == slug))
    return result.scalars().first()


async def list_products(session: AsyncSession) -> Sequence[Product]:
    result = await session.execute(select(Product).order_by(Product.display_name))
    return result.scalars().all()


async def count_products(session: AsyncSession) -> int:
    """Every product row, active or not."""
    return int(await session.scalar(select(func.count()).select_from(Product)) or 0)


async def products_by_id(
    session: AsyncSession, product_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, Product]:
    """Several products in one query, for attaching product context to a page of rows."""
    ids = list(set(product_ids))
    if not ids:
        return {}
    result = await session.execute(select(Product).where(Product.id.in_(ids)))
    return {product.id: product for product in result.scalars().all()}


class UnknownReceivingNumberError(Exception):
    """No active product owns the number that received this message ([D40], [D45]).

    Raised rather than defaulted. Routing a customer to a product that does not own the
    number they messaged would put them in the wrong product's conversation, with the
    wrong knowledge and the wrong voice — and nothing in the payload could later reveal
    the mistake.
    """

    def __init__(self, phone_number_id: str | None) -> None:
        super().__init__(
            f"No active product is configured for receiving number {phone_number_id!r}."
            if phone_number_id
            else "The webhook delivery carried no receiving phone number ID."
        )
        self.phone_number_id = phone_number_id


async def product_for_receiving_number(
    session: AsyncSession, phone_number_id: str | None
) -> Product:
    """The product that owns the WhatsApp number a message arrived on ([D40]).

    **The only inbound routing rule.** Not the sender's number, not the WABA — which is
    shared across products, so it identifies nothing — not campaign history, not the
    template, not the sole active product, and not whichever product happens to be
    first.

    Fails closed. A missing or unrecognised number raises, and the caller records the
    delivery without acting on it: an unroutable message is a configuration problem to
    fix, never a customer to guess about.
    """
    if not phone_number_id:
        raise UnknownReceivingNumberError(None)

    product = (
        (
            await session.execute(
                select(Product).where(
                    Product.whatsapp_phone_number_id == phone_number_id,
                    Product.is_active.is_(True),
                )
            )
        )
        .scalars()
        .first()
    )

    if product is None:
        raise UnknownReceivingNumberError(phone_number_id)
    return product


async def get_segment(session: AsyncSession, segment_id: uuid.UUID) -> Segment | None:
    return await session.get(Segment, segment_id)


async def get_segment_by_slug(
    session: AsyncSession, *, product_id: uuid.UUID, slug: str
) -> Segment | None:
    """A segment by its slug, looked up only within one product.

    Segment slugs are unique per product, not globally ([D49]) — two products may both
    have a "government" segment, and they are different things.
    """
    result = await session.execute(
        select(Segment).where(Segment.product_id == product_id, Segment.slug == slug)
    )
    return result.scalars().first()


async def list_segments(session: AsyncSession, product_id: uuid.UUID) -> Sequence[Segment]:
    result = await session.execute(
        select(Segment)
        .where(Segment.product_id == product_id)
        .order_by(Segment.display_order, Segment.display_name)
    )
    return result.scalars().all()


__all__ = [
    "UnknownReceivingNumberError",
    "count_products",
    "get_by_slug",
    "get_product",
    "get_segment",
    "get_segment_by_slug",
    "list_products",
    "list_segments",
    "product_for_receiving_number",
    "products_by_id",
]
