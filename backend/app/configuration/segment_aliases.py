"""Idempotent remapping of obsolete segment slugs onto their canonical replacements.

The Phase-27 Life Herba seed used ``healthcare-professionals`` and ``wellness-centers``.
Launch confirmed ``healthcare-practitioners`` and ``wellness-centres``. The catalog creates
segments by slug and never deletes, so both can coexist after a re-configure.

This module collapses the obsolete rows onto the canonical ones for the Life Herba
product only: references are remapped first, then the obsolete segment is removed.
Other products are untouched. Safe to re-run.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models import (
    AIConfiguration,
    Audience,
    Campaign,
    Conversation,
    KnowledgeBase,
    Product,
    Segment,
)

#: Obsolete Life Herba slug → (canonical slug, canonical display name).
LIFE_HERBA_SEGMENT_RENAMES: Mapping[str, tuple[str, str]] = {
    "healthcare-professionals": ("healthcare-practitioners", "Healthcare Practitioners"),
    "wellness-centers": ("wellness-centres", "Wellness Centres"),
}

LIFE_HERBA_SLUG = "life-herba"


async def reconcile_life_herba_segment_aliases(session: AsyncSession) -> list[str]:
    """Remap obsolete Life Herba segments onto the canonical four. Idempotent."""
    result = await session.execute(select(Product).where(Product.slug == LIFE_HERBA_SLUG))
    product = result.scalar_one_or_none()
    if product is None:
        return []

    changes: list[str] = []
    for obsolete, (canonical, display_name) in LIFE_HERBA_SEGMENT_RENAMES.items():
        changes.extend(
            await _reconcile_one(
                session,
                product_id=product.id,
                obsolete_slug=obsolete,
                canonical_slug=canonical,
                canonical_display_name=display_name,
            )
        )
    return changes


async def _reconcile_one(
    session: AsyncSession,
    *,
    product_id: uuid.UUID,
    obsolete_slug: str,
    canonical_slug: str,
    canonical_display_name: str,
) -> list[str]:
    obsolete = await _segment(session, product_id, obsolete_slug)
    if obsolete is None:
        return []

    canonical = await _segment(session, product_id, canonical_slug)
    label = f"life-herba: segment {obsolete_slug}"

    if canonical is None:
        obsolete.slug = canonical_slug
        obsolete.display_name = canonical_display_name
        await session.flush()
        return [f"{label} renamed to {canonical_slug}"]

    if obsolete.id == canonical.id:
        return []

    remapped = await _remap_references(session, from_id=obsolete.id, to_id=canonical.id)
    # The canonical row already owns (or will own) the AI override from the catalog.
    # Drop the obsolete override rather than colliding on (product_id, segment_id).
    await session.execute(
        delete(AIConfiguration).where(AIConfiguration.segment_id == obsolete.id)
    )
    await session.delete(obsolete)
    await session.flush()

    detail = f"{label} remapped to {canonical_slug}"
    if remapped:
        detail = f"{detail} ({remapped} reference(s))"
    return [detail]


async def _segment(
    session: AsyncSession, product_id: uuid.UUID, slug: str
) -> Segment | None:
    result = await session.execute(
        select(Segment).where(Segment.product_id == product_id, Segment.slug == slug)
    )
    return result.scalar_one_or_none()


async def _remap_references(
    session: AsyncSession, *, from_id: uuid.UUID, to_id: uuid.UUID
) -> int:
    """Move RESTRICT FKs off the obsolete segment before it is deleted."""
    statements = (
        update(Audience).where(Audience.segment_id == from_id).values(segment_id=to_id),
        update(Campaign).where(Campaign.segment_id == from_id).values(segment_id=to_id),
        update(Conversation)
        .where(Conversation.segment_id == from_id)
        .values(segment_id=to_id),
        update(KnowledgeBase)
        .where(KnowledgeBase.segment_id == from_id)
        .values(segment_id=to_id),
    )
    total = 0
    for statement in statements:
        result = await session.execute(statement)
        total += int(getattr(result, "rowcount", 0) or 0)
    return total


__all__ = [
    "LIFE_HERBA_SEGMENT_RENAMES",
    "LIFE_HERBA_SLUG",
    "reconcile_life_herba_segment_aliases",
]
