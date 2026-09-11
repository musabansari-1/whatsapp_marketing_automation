"""Template ownership is explicit application data ([D57])

One Meta App and **one shared WABA** give one template pool, and Meta records no owner on
a template. So `whatsapp_templates.product_id` is nullable by design: NULL is a provider
template the sync discovered and nobody has assigned yet, and no campaign may use one.
Phase 23 had listed it as debt; this records the decision that it is not.

**No data changes.** Existing assignments stay exactly as they are — Phase 18 backfilled
every template that existed then to Academicae, and nothing here reassigns, clears or
infers anything.

**The uniqueness rule moves from provider naming to application ownership.**

Dropped: `UNIQUE (name, language)` over every row. It restated a Meta naming rule rather
than an application invariant, and it had a real failure mode: Meta lets a deleted
template's name be issued again under a new ID, and the sync's insert of that new artifact
would collide with the old row and fail the whole batch.

Added: `UNIQUE (product_id, name, language) WHERE product_id IS NOT NULL`. Within one
product a name and language identify one template, so "send `welcome` in `en`" is never
ambiguous for a campaign. Partial, because unassigned provider artifacts are nobody's
namespace.

Provider identity is unchanged: `provider_template_id` stays unique, so one Meta artifact
is still one row and is never copied into a row per product.

Revision ID: b3c8d1e5f927
Revises: a4e81b2f7c93
Create Date: 2026-09-10
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "b3c8d1e5f927"
down_revision = "a4e81b2f7c93"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("uq_whatsapp_templates_name_language", "whatsapp_templates", type_="unique")
    op.create_index(
        "uq_whatsapp_templates_product_name_language",
        "whatsapp_templates",
        ["product_id", "name", "language"],
        unique=True,
        postgresql_where=sa.text("product_id IS NOT NULL"),
    )


def downgrade() -> None:
    """Restore the global `(name, language)` rule — **when the data still allows it.**

    It fails, with an integrity error and nothing changed, once two rows share a name and
    language: a re-issued Meta template alongside its deleted predecessor, or two products
    each owning a template of that name. Nothing here deletes either row to make room.
    Which artifact to discard, and whose campaigns lose their template, is not a decision a
    downgrade can make.
    """
    op.drop_index("uq_whatsapp_templates_product_name_language", table_name="whatsapp_templates")
    op.create_unique_constraint(
        "uq_whatsapp_templates_name_language", "whatsapp_templates", ["name", "language"]
    )
