"""Product and segment queries (§28).

Products are rows ([D49]). Nothing here knows how many exist or what any is called, and
there is no default product: every lookup is by an identifier the caller already holds —
an ID, a route slug, or the WhatsApp number a message arrived on.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence

from sqlalchemy import ColumnElement, func, select
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


async def slugs_sharing_phone_number(session: AsyncSession, product: Product) -> tuple[str, ...]:
    """Other products holding this product's WhatsApp number, active or not.

    Wider than `uq_products_active_whatsapp_number`, which only covers active products. An
    inactive product holding the same number is a collision waiting for its activation.
    """
    if not product.whatsapp_phone_number_id:
        return ()
    return await _other_slugs(
        session, product, Product.whatsapp_phone_number_id == product.whatsapp_phone_number_id
    )


async def slugs_sharing_calendar(session: AsyncSession, product: Product) -> tuple[str, ...]:
    """Other products holding this product's calendar, active or not ([D43])."""
    if not product.google_calendar_id:
        return ()
    return await _other_slugs(
        session, product, Product.google_calendar_id == product.google_calendar_id
    )


async def slugs_sharing_meeting_url(session: AsyncSession, product: Product) -> tuple[str, ...]:
    """Other products holding this product's meeting link, active or not.

    A shared room would put two products' demos in one call, so the check mirrors
    the calendar one ([D43]).
    """
    if not product.meeting_url:
        return ()
    return await _other_slugs(session, product, Product.meeting_url == product.meeting_url)


async def _other_slugs(
    session: AsyncSession, product: Product, condition: ColumnElement[bool]
) -> tuple[str, ...]:
    result = await session.execute(
        select(Product.slug).where(condition, Product.id != product.id).order_by(Product.slug)
    )
    return tuple(result.scalars().all())


async def list_whatsapp_business_accounts(session: AsyncSession) -> list[str]:
    """Every WhatsApp Business Account a product is configured under, once each.

    One shared account today, so normally a single value. Template sync reads each one; a
    product later moved to an account of its own is configuration, not a redesign.
    """
    result = await session.execute(
        select(Product.whatsapp_business_account_id)
        .where(Product.whatsapp_business_account_id.is_not(None))
        .distinct()
        .order_by(Product.whatsapp_business_account_id)
    )
    return [account for account in result.scalars().all() if account]


__all__ = [
    "UnknownReceivingNumberError",
    "count_products",
    "list_whatsapp_business_accounts",
    "slugs_sharing_calendar",
    "slugs_sharing_meeting_url",
    "slugs_sharing_phone_number",
    "get_by_slug",
    "get_product",
    "get_segment",
    "get_segment_by_slug",
    "list_products",
    "list_segments",
    "product_for_receiving_number",
    "products_by_id",
]
