"""WhatsApp template synchronization and ownership ([D6], [D57]).

Two concerns that must not be confused.

**Synchronization is WABA-level.** One Meta App and one shared WABA give one template
pool, so there is one sync over it — not a sync per product pretending the same pool is
three. It records what Meta says about each provider artifact and nothing about who
owns it.

**Ownership is application data, assigned explicitly.** A newly discovered template has
no product, and stays that way until someone gives it one. Nothing here infers an owner
from a name, the WABA, a phone number, the sole active product or campaign history.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.campaign import (
    TemplateAssignmentError,
    TemplateMediaMissingError,
    TemplateNotApprovedError,
    TemplateNotFoundError,
    assert_header_media_available,
    assert_template_owned_by,
    needs_assignment,
)
from app.integrations.whatsapp import ApprovedTemplate
from app.persistence.models import Product, WhatsAppTemplate


async def assign_to_product(
    session: AsyncSession, *, template_id: uuid.UUID, product_id: uuid.UUID
) -> WhatsAppTemplate:
    """Give an unassigned provider template to one product ([D57]).

    The only way a template gains an owner. Phase 26 puts an admin screen in front of it;
    the rule lives here so that screen cannot differ from it.

    Refuses when the template or the product does not exist, when another product already
    owns the template, and when the target product already owns a template with the same
    name and language — which would make "send `welcome` in `en`" mean two different
    things within one product. Assigning a template to the product that already owns it
    succeeds and changes nothing.

    The template row is locked for the check, so two admins assigning at once cannot both
    pass it. `uq_whatsapp_templates_product_name_language` is the backstop.
    """
    template = await session.get(WhatsAppTemplate, template_id, with_for_update=True)
    if template is None:
        raise TemplateAssignmentError("Template not found")

    product = await session.get(Product, product_id)
    if product is None:
        raise TemplateAssignmentError("Product not found")

    if not needs_assignment(current_owner=template.product_id, target=product_id):
        return template

    clash = (
        await session.execute(
            select(WhatsAppTemplate.id).where(
                WhatsAppTemplate.product_id == product_id,
                WhatsAppTemplate.name == template.name,
                WhatsAppTemplate.language == template.language,
                WhatsAppTemplate.id != template.id,
            )
        )
    ).first()
    if clash is not None:
        raise TemplateAssignmentError(
            f"{product.display_name} already has a template named {template.name!r} "
            f"in {template.language!r}"
        )

    template.product_id = product_id
    await session.flush()
    return template


async def set_header_media(
    session: AsyncSession, *, template_id: uuid.UUID, media_url: str | None
) -> WhatsAppTemplate:
    """Choose the media a template's header carries, or clear it with None.

    The one way `header_media_url` is written, and it is explicit for the same reason
    ownership is: the sample Meta stores beside an approved template is a placeholder in
    the manager, not the asset a campaign should deliver, so nothing may infer this from
    a sync, a product's defaults or a campaign's history.

    Clearing it (None) is allowed and leaves a media-header template unsendable, which
    the send path then refuses by name rather than discovering through a permanent
    132012 against every recipient.
    """
    template = await session.get(WhatsAppTemplate, template_id, with_for_update=True)
    if template is None:
        raise TemplateNotFoundError("Template not found")

    template.header_media_url = media_url
    await session.flush()
    return template


async def set_first_reply_template(
    session: AsyncSession, *, product_id: uuid.UUID, template_id: uuid.UUID
) -> WhatsAppTemplate:
    """Select the template sent once to a first-time sender of this product.

    The only writer of `products.first_reply_template_id`, explicit like
    ownership: the template must already belong to this product ([D57]), Meta
    must have approved it ([D6]), and a media header must have its asset, or
    the welcome fails the same way a broadcast would. Clearing (None) is done
    with `clear_first_reply_template`, which disables the welcome.
    """
    from app.persistence.models.campaign import META_STATUS_APPROVED

    product = await session.get(Product, product_id)
    if product is None:
        raise TemplateAssignmentError("Product not found")

    template = await session.get(WhatsAppTemplate, template_id)
    if template is None:
        raise TemplateNotFoundError("Template not found")

    assert_template_owned_by(
        template_product_id=template.product_id, campaign_product_id=product_id
    )
    if template.provider_status != META_STATUS_APPROVED:
        raise TemplateNotApprovedError(
            f"Template status is {template.provider_status!r}; only "
            f"{META_STATUS_APPROVED!r} templates may be used"
        )
    try:
        assert_header_media_available(
            header_type=template.header_type, media_url=template.header_media_url
        )
    except TemplateMediaMissingError as exc:
        raise TemplateAssignmentError(str(exc)) from exc

    product.first_reply_template_id = template.id
    await session.flush()
    return template


async def clear_first_reply_template(session: AsyncSession, *, product_id: uuid.UUID) -> None:
    """Disable the first-reply welcome: first-time senders get the AI reply."""
    product = await session.get(Product, product_id)
    if product is None:
        raise TemplateAssignmentError("Product not found")
    product.first_reply_template_id = None
    await session.flush()


async def list_owned(session: AsyncSession, *, product_id: uuid.UUID) -> Sequence[WhatsAppTemplate]:
    """Every template one product owns, whatever Meta's status."""
    result = await session.execute(
        select(WhatsAppTemplate)
        .where(WhatsAppTemplate.product_id == product_id)
        .order_by(WhatsAppTemplate.name, WhatsAppTemplate.language)
    )
    return result.scalars().all()


async def list_unassigned(session: AsyncSession) -> Sequence[WhatsAppTemplate]:
    """Shared-WABA templates no product owns yet ([D57])."""
    result = await session.execute(
        select(WhatsAppTemplate)
        .where(WhatsAppTemplate.product_id.is_(None))
        .order_by(WhatsAppTemplate.name, WhatsAppTemplate.language)
    )
    return result.scalars().all()


async def upsert_many(session: AsyncSession, templates: Sequence[ApprovedTemplate]) -> int:
    """Store template metadata exactly as Meta reported it.

    Upserted on the provider template ID, so re-running the sync updates rather than
    duplicates and a template whose approval Meta later revokes has its status
    corrected here. That matters: a campaign filters on the stored status, so a stale
    APPROVED would let a revoked template be broadcast.

    Nothing in this function decides approval ([D6]); it records what arrived.

    **`product_id` is absent from both the insert and the update clause, deliberately.**

    One Meta App and one shared WABA means this returns every product's templates in a
    single undifferentiated list, and Meta's records carry no product field — so the sync
    cannot attribute one without inventing a mapping, which is what [D40] rules out.
    Leaving the column out of the update means a re-sync never reassigns or clears an
    ownership someone did establish; adding it to `set_` would silently overwrite them
    all on the next scheduled run. Ownership is assigned outside this path, or not at all.

    `header_media_url` is excluded for the same reason, and more strongly: Meta's record
    carries only a sample image, so a sync that wrote it would overwrite the asset an
    operator chose with Meta's placeholder — silently changing what every later
    campaign of this product sends. Only `header_type` is synchronized, because that is
    Meta's to decide; the media is ours.
    """
    if not templates:
        return 0

    now = datetime.now(UTC)
    statement = pg_insert(WhatsAppTemplate).values(
        [
            {
                "name": template.name,
                "language": template.language,
                "provider_template_id": template.provider_template_id,
                "provider_status": template.provider_status,
                "category": template.category,
                "variable_count": template.variable_count,
                "body_preview": template.body_preview,
                # Meta's own view of the shape. Corrected on every sync, so a template
                # whose header was changed in the manager stops being sent as the old
                # shape — which Meta refuses permanently.
                "header_type": template.header_type,
                "synced_at": now,
            }
            for template in templates
        ]
    )
    statement = statement.on_conflict_do_update(
        index_elements=[WhatsAppTemplate.provider_template_id],
        set_={
            "name": statement.excluded.name,
            "language": statement.excluded.language,
            "provider_status": statement.excluded.provider_status,
            "category": statement.excluded.category,
            "variable_count": statement.excluded.variable_count,
            "body_preview": statement.excluded.body_preview,
            "header_type": statement.excluded.header_type,
            "synced_at": statement.excluded.synced_at,
        },
    )
    await session.execute(statement)
    await session.flush()
    return len(templates)
