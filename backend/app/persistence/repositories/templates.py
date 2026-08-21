"""WhatsApp template synchronization ([D6])."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.whatsapp import ApprovedTemplate
from app.persistence.models import WhatsAppTemplate


async def upsert_many(session: AsyncSession, templates: Sequence[ApprovedTemplate]) -> int:
    """Store template metadata exactly as Meta reported it.

    Upserted on the provider template ID, so re-running the sync updates rather than
    duplicates and a template whose approval Meta later revokes has its status
    corrected here. That matters: a campaign filters on the stored status, so a stale
    APPROVED would let a revoked template be broadcast.

    Nothing in this function decides approval ([D6]); it records what arrived.
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
            "synced_at": statement.excluded.synced_at,
        },
    )
    await session.execute(statement)
    await session.flush()
    return len(templates)
