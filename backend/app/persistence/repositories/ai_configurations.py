"""AI configuration lookup and resolution (§28.4, Phases 22 and 27)."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.ai_config import PlatformDefaults, ResolvedAIConfig, resolve
from app.persistence.models import AIConfiguration, Segment

if TYPE_CHECKING:
    # Type-only: a repository does not depend on configuration at runtime, and the
    # caller passes the values in.
    from app.core.config import Settings


async def get_product_default(
    session: AsyncSession, product_id: uuid.UUID
) -> AIConfiguration | None:
    """The product's own row, identified by having no segment."""
    result = await session.execute(
        select(AIConfiguration).where(
            AIConfiguration.product_id == product_id,
            AIConfiguration.segment_id.is_(None),
        )
    )
    return result.scalars().first()


async def get_segment_override(
    session: AsyncSession, product_id: uuid.UUID, segment_id: uuid.UUID
) -> AIConfiguration | None:
    """A segment's sparse override, if it has one.

    Absence is the normal case, not a failure: SustainX's segments exist for marketing
    targeting and share one behaviour, so none of them has a row (§28.4).
    """
    result = await session.execute(
        select(AIConfiguration).where(
            AIConfiguration.product_id == product_id,
            AIConfiguration.segment_id == segment_id,
        )
    )
    return result.scalars().first()


async def segment_overrides_by_slug(
    session: AsyncSession, product_id: uuid.UUID
) -> dict[str, AIConfiguration]:
    """Every segment override a product holds, keyed by the segment's slug."""
    result = await session.execute(
        select(Segment.slug, AIConfiguration)
        .join(Segment, AIConfiguration.segment_id == Segment.id)
        .where(AIConfiguration.product_id == product_id)
    )
    return dict(result.tuples().all())


async def resolve_for(
    session: AsyncSession,
    *,
    product_id: uuid.UUID,
    segment_id: uuid.UUID | None,
    defaults: PlatformDefaults,
) -> ResolvedAIConfig:
    """The configuration one turn runs with (§28.4).

    Segment override, then product default, then — for operational settings only — the
    platform baseline. Policy comes from the product's own rows or not at all; Phase 27
    removed the rule that let the sole active product use the environment's.

    Raises `AIConfigurationError` when a product's policy cannot be resolved. The caller
    fails the turn closed: replying with another product's prompt would be worse than not
    replying.
    """
    product = await get_product_default(session, product_id)
    segment = (
        await get_segment_override(session, product_id, segment_id)
        if segment_id is not None
        else None
    )
    return resolve(segment=segment, product=product, defaults=defaults)


def platform_defaults_from_settings(settings: Settings) -> PlatformDefaults:
    """The operational baselines every product inherits. There is no policy among them."""
    return PlatformDefaults(
        model=settings.openrouter_model,
        rag_top_k=settings.rag_top_k,
        rag_min_similarity=settings.rag_min_similarity,
        rag_max_context_chars=settings.rag_max_context_chars,
        history_turns=settings.conversation_history_turns,
        max_tool_rounds=settings.ai_max_tool_rounds,
        fallback_message=settings.msg_rag_fallback,
    )


__all__ = [
    "get_product_default",
    "get_segment_override",
    "platform_defaults_from_settings",
    "resolve_for",
    "segment_overrides_by_slug",
]
