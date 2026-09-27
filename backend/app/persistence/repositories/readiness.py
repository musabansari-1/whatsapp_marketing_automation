"""What product readiness needs to know, read in one place (Phase 27).

The rules are pure and live in `app.domain.readiness`. This gathers the facts they judge —
one product row, its segments and AI rows, whether its number or calendar is held by
another product, how many usable templates it owns, and the state of its knowledge.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.campaign import TemplateFacts, TemplateNotApprovedError, assert_template_usable
from app.domain.enums import DocumentStatus, TemplateCategory
from app.domain.readiness import PlatformInputs, ProductFacts, ProductReadiness, evaluate
from app.persistence.models import KnowledgeBase, KnowledgeDocument, Product, WhatsAppTemplate
from app.persistence.repositories import ai_configurations as ai_config_repo
from app.persistence.repositories import products as product_repo
from app.persistence.repositories import templates as template_repo

if TYPE_CHECKING:
    from app.core.config import Settings


class UnknownProductError(LookupError):
    """A readiness or activation command named a slug no product has."""


def platform_inputs_from_settings(settings: Settings) -> PlatformInputs:
    return PlatformInputs(
        ai_defaults=ai_config_repo.platform_defaults_from_settings(settings),
        slot_settings=settings,
        retrieval_enabled=settings.ai_retrieval_enabled,
        production=settings.is_production,
    )


async def gather_facts(session: AsyncSession, product: Product) -> ProductFacts:
    segments = await product_repo.list_segments(session, product.id)
    documents = await _document_counts(session, product)
    return ProductFacts(
        slug=product.slug,
        display_name=product.display_name,
        is_active=product.is_active,
        audience_column_schema=product.audience_column_schema,
        whatsapp_phone_number_id=product.whatsapp_phone_number_id,
        whatsapp_business_account_id=product.whatsapp_business_account_id,
        whatsapp_access_token_secret_name=product.whatsapp_access_token_secret_name,
        google_calendar_id=product.google_calendar_id,
        meeting_url=product.meeting_url,
        slot_overrides=product,
        segment_slugs=tuple(segment.slug for segment in segments),
        product_ai=await ai_config_repo.get_product_default(session, product.id),
        segment_ai=await ai_config_repo.segment_overrides_by_slug(session, product.id),
        conversion_mode=product.conversion_mode,
        registration_url=product.registration_url,
        phone_number_also_on=await product_repo.slugs_sharing_phone_number(session, product),
        calendar_also_on=await product_repo.slugs_sharing_calendar(session, product),
        meeting_url_also_on=await product_repo.slugs_sharing_meeting_url(session, product),
        usable_marketing_templates=sum(
            1
            for template in await template_repo.list_owned(session, product_id=product.id)
            if _usable(template)
        ),
        ready_documents=documents.get(DocumentStatus.READY, 0),
        failed_documents=documents.get(DocumentStatus.FAILED, 0),
    )


async def evaluate_product(
    session: AsyncSession, product: Product, platform: PlatformInputs
) -> ProductReadiness:
    return evaluate(await gather_facts(session, product), platform)


async def evaluate_products(
    session: AsyncSession, platform: PlatformInputs, slugs: Sequence[str] = ()
) -> list[ProductReadiness]:
    """Readiness for the named products, or for every product when none are named.

    Ordered by slug, so the report reads the same on every run.
    """
    if slugs:
        products = []
        for slug in slugs:
            product = await product_repo.get_by_slug(session, slug)
            if product is None:
                raise UnknownProductError(f"No product has the slug {slug!r}")
            products.append(product)
    else:
        products = list(await product_repo.list_products(session))

    return [
        await evaluate_product(session, product, platform)
        for product in sorted(products, key=lambda p: p.slug)
    ]


def _usable(template: WhatsAppTemplate) -> bool:
    """The rule campaign creation enforces, asked as a question ([D6]). Ownership is
    already given: these are the product's own templates."""
    try:
        assert_template_usable(
            TemplateFacts(
                provider_status=template.provider_status,
                is_marketing=template.category is TemplateCategory.MARKETING,
            )
        )
    except TemplateNotApprovedError:
        return False
    return True


async def _document_counts(session: AsyncSession, product: Product) -> dict[DocumentStatus, int]:
    result = await session.execute(
        select(KnowledgeDocument.status, func.count())
        .join(KnowledgeBase, KnowledgeDocument.knowledge_base_id == KnowledgeBase.id)
        .where(KnowledgeBase.product_id == product.id)
        .group_by(KnowledgeDocument.status)
    )
    return {status: int(count) for status, count in result.tuples().all()}


__all__ = [
    "UnknownProductError",
    "evaluate_product",
    "evaluate_products",
    "gather_facts",
    "platform_inputs_from_settings",
]
