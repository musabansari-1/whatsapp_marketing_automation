"""AI configuration lookup and resolution (§28.4, Phase 22)."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.ai_config import PlatformDefaults, ResolvedAIConfig, resolve
from app.persistence.models import AIConfiguration, Product

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


async def _legacy_policy_allowed(session: AsyncSession, product_id: uuid.UUID) -> bool:
    """Whether this product may still use the platform's environment-configured policy.

    True only while **exactly one product is active** and that product is this one. The
    rule is on the count, never on identity: there is no slug, name, UUID or "first"
    anywhere in it, so it cannot quietly become "default to Academicae". The moment a
    second product is activated it is false for every product at once, including the
    original.

    Transitional. Phase 27 seeds real per-product configuration, after which no product
    reaches this path and the rule can be deleted.
    """
    active = list(
        (await session.execute(select(Product.id).where(Product.is_active.is_(True)))).scalars()
    )
    return len(active) == 1 and active[0] == product_id


async def resolve_for(
    session: AsyncSession,
    *,
    product_id: uuid.UUID,
    segment_id: uuid.UUID | None,
    defaults: PlatformDefaults,
) -> ResolvedAIConfig:
    """The configuration one turn runs with (§28.4).

    Segment override, then product default, then — for operational settings only, or for
    policy while the legacy compatibility rule holds — the platform baseline.

    Raises `AIConfigurationError` when a product's policy cannot be resolved. The caller
    fails the turn closed: replying with another product's prompt would be worse than
    not replying.
    """
    product = await get_product_default(session, product_id)
    segment = (
        await get_segment_override(session, product_id, segment_id)
        if segment_id is not None
        else None
    )

    return resolve(
        segment=segment,
        product=product,
        defaults=defaults,
        legacy_policy_allowed=await _legacy_policy_allowed(session, product_id),
    )


def platform_defaults_from_settings(settings: Settings) -> PlatformDefaults:
    """Operational baselines, plus the legacy policy the sole product may still use.

    Reads the existing environment configuration so [D54] holds unchanged: while
    Academicae is the only active product and has configured no row of its own, it keeps
    behaving exactly as it did.
    """
    return PlatformDefaults(
        model=settings.openrouter_model,
        rag_top_k=settings.rag_top_k,
        rag_min_similarity=settings.rag_min_similarity,
        rag_max_context_chars=settings.rag_max_context_chars,
        history_turns=settings.conversation_history_turns,
        max_tool_rounds=settings.ai_max_tool_rounds,
        legacy_system_prompt=settings.ai_system_prompt,
        legacy_qualification_required_fields=settings.qualification_required_fields,
        legacy_fallback_message=settings.msg_rag_fallback,
    )


__all__ = [
    "get_product_default",
    "get_segment_override",
    "platform_defaults_from_settings",
    "resolve_for",
]
