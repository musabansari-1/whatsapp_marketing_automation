"""Idempotent remapping of obsolete segment slugs onto their canonical replacements.

Life Herba: Phase-27 seeded ``healthcare-professionals`` and ``wellness-centers``.
Launch confirmed ``healthcare-practitioners`` and ``wellness-centres``.

SustainX: Phase-27 seeded ``government`` and ``industry``. Launch confirmed five
canonical segments. Clear renames are remapped; ambiguous experimental segments are
never collapsed into Industries without inspection.

The catalog creates segments by slug and never deletes, so obsolete and canonical rows
can coexist after a re-configure. This module collapses clear renames for the named
product only: references are remapped first, then the obsolete segment is removed.
Other products are untouched. Safe to re-run.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping

from sqlalchemy import delete, func, select, update
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

#: Clear SustainX renames only. Ambiguous experimental audiences are never auto-mapped
#: into Industries without inspecting their records.
SUSTAINX_SEGMENT_RENAMES: Mapping[str, tuple[str, str]] = {
    "government": ("government-bodies", "Government Bodies"),
    "industry": ("industries", "Industries"),
    "universities": ("institutions", "Institutions"),
}

#: Experimental SustainX slugs that must not be collapsed automatically.
SUSTAINX_AMBIGUOUS_OBSOLETE_SLUGS: frozenset[str] = frozenset(
    {
        "corporates",
        "msmes",
        "developers-infrastructure",
        "manufacturing-industries",
        "services",
        "center-of-excellence",
        "centre-of-excellence",
        "training",
    }
)

SUSTAINX_SLUG = "sustainx"

SUSTAINX_CANONICAL_SLUGS: frozenset[str] = frozenset(
    {
        "industries",
        "institutions",
        "government-bodies",
        "students",
        "professionals-executives",
    }
)


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
                product_slug=LIFE_HERBA_SLUG,
                obsolete_slug=obsolete,
                canonical_slug=canonical,
                canonical_display_name=display_name,
            )
        )
    return changes


async def reconcile_sustainx_segment_aliases(session: AsyncSession) -> list[str]:
    """Remap clear SustainX renames; report or remove ambiguous leftovers. Idempotent."""
    result = await session.execute(select(Product).where(Product.slug == SUSTAINX_SLUG))
    product = result.scalar_one_or_none()
    if product is None:
        return []

    changes: list[str] = []
    for obsolete, (canonical, display_name) in SUSTAINX_SEGMENT_RENAMES.items():
        changes.extend(
            await _reconcile_one(
                session,
                product_id=product.id,
                product_slug=SUSTAINX_SLUG,
                obsolete_slug=obsolete,
                canonical_slug=canonical,
                canonical_display_name=display_name,
            )
        )

    remaining = await session.execute(
        select(Segment).where(Segment.product_id == product.id).order_by(Segment.slug)
    )
    for segment in remaining.scalars():
        if segment.slug in SUSTAINX_CANONICAL_SLUGS:
            continue
        if segment.slug in SUSTAINX_SEGMENT_RENAMES:
            # Should already have been remapped; leave a trail if not.
            continue
        refs = await _reference_count(session, segment.id)
        label = f"{SUSTAINX_SLUG}: segment {segment.slug}"
        if refs > 0:
            changes.append(
                f"{label} left in place — ambiguous historical data "
                f"({refs} reference(s)); manual classification required"
            )
            continue
        await session.execute(
            delete(AIConfiguration).where(AIConfiguration.segment_id == segment.id)
        )
        await session.delete(segment)
        await session.flush()
        changes.append(f"{label} removed (no references; not in the canonical launch set)")
    return changes


async def _reconcile_one(
    session: AsyncSession,
    *,
    product_id: uuid.UUID,
    product_slug: str,
    obsolete_slug: str,
    canonical_slug: str,
    canonical_display_name: str,
) -> list[str]:
    obsolete = await _segment(session, product_id, obsolete_slug)
    if obsolete is None:
        return []

    canonical = await _segment(session, product_id, canonical_slug)
    label = f"{product_slug}: segment {obsolete_slug}"

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
    await session.execute(delete(AIConfiguration).where(AIConfiguration.segment_id == obsolete.id))
    await session.delete(obsolete)
    await session.flush()

    detail = f"{label} remapped to {canonical_slug}"
    if remapped:
        detail = f"{detail} ({remapped} reference(s))"
    return [detail]


async def _segment(session: AsyncSession, product_id: uuid.UUID, slug: str) -> Segment | None:
    result = await session.execute(
        select(Segment).where(Segment.product_id == product_id, Segment.slug == slug)
    )
    return result.scalar_one_or_none()


async def _remap_references(session: AsyncSession, *, from_id: uuid.UUID, to_id: uuid.UUID) -> int:
    """Move RESTRICT FKs off the obsolete segment before it is deleted."""
    statements = (
        update(Audience).where(Audience.segment_id == from_id).values(segment_id=to_id),
        update(Campaign).where(Campaign.segment_id == from_id).values(segment_id=to_id),
        update(Conversation).where(Conversation.segment_id == from_id).values(segment_id=to_id),
        update(KnowledgeBase).where(KnowledgeBase.segment_id == from_id).values(segment_id=to_id),
    )
    total = 0
    for statement in statements:
        result = await session.execute(statement)
        total += int(getattr(result, "rowcount", 0) or 0)
    return total


async def _reference_count(session: AsyncSession, segment_id: uuid.UUID) -> int:
    total = 0
    for model in (Audience, Campaign, Conversation, KnowledgeBase, AIConfiguration):
        count = await session.scalar(
            select(func.count()).select_from(model).where(model.segment_id == segment_id)
        )
        total += int(count or 0)
    return total


__all__ = [
    "LIFE_HERBA_SEGMENT_RENAMES",
    "LIFE_HERBA_SLUG",
    "SUSTAINX_AMBIGUOUS_OBSOLETE_SLUGS",
    "SUSTAINX_CANONICAL_SLUGS",
    "SUSTAINX_SEGMENT_RENAMES",
    "SUSTAINX_SLUG",
    "reconcile_life_herba_segment_aliases",
    "reconcile_sustainx_segment_aliases",
]
