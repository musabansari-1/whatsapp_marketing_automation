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
