"""Scope existing data to a product (Phase 18, §28)

Three things, in order.

**The existing product is Academicae.** Phase 17 seeded it with placeholder values
because its commercial name had not been supplied. This renames that same row —
the UUID is unchanged, and nothing else in the platform may hold a second row for it.
The update is idempotent and guarded on the placeholder slug, so a manual rename made
in the meantime is not clobbered.

**Every scoped table gains a product reference, and existing rows are backfilled to
Academicae.** Existing data is single-product data by definition, so this needs no
inference: it does not read campaign names, template names, phone numbers or
environment strings to decide.

**Nothing becomes NOT NULL.** The columns stay nullable because the write paths cannot
yet supply a product: `POST /audiences/upload` and `POST /knowledge-bases` take no
product, template sync has no product context, and inbound webhooks cannot route until
Phase 23. Setting NOT NULL here would break every one of those inserts and take the
[D54] Academicae guard with them. Each column's closing phase is recorded in
`tests/test_product_scope.py`, which fails if a column is added without one.

**No uniqueness constraint changes, deliberately.** `knowledge_bases.name` and
`whatsapp_templates (name, language)` keep their existing global uniqueness. Replacing
either with a product-scoped rule while rows can still be written with a NULL product
would *weaken* today's guarantee: two NULL-product rows would no longer collide, because
SQL treats NULLs as distinct. Those changes belong to the phases that make the writers
product-aware — Phase 26 for knowledge, Phase 23 for templates — where the old and new
rules can be swapped in one step.

Revision ID: e5c74a1b8d09
Revises: d3f8b21c60ae
Create Date: 2026-09-08
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "e5c74a1b8d09"
down_revision = "d3f8b21c60ae"
branch_labels = None
depends_on = None

# The row Phase 17 seeded. Referenced here so the backfill is unambiguous; resolving
# it as "the only product" would break the moment a second one exists.
ACADEMICAE_ID = "00000000-0000-4000-8000-000000000001"
PLACEHOLDER_SLUG = "product-1"
ACADEMICAE_SLUG = "academicae"
ACADEMICAE_NAME = "Academicae"

# Tables gaining a product reference, and whether they also gain a segment.
#
# `leads` deliberately has no segment ([D48]): a lead may belong to several segments
# within a product, so a single column would be misleading and would eventually be
# mistaken for authority. Membership is audience_contacts -> audiences.segment_id.
SCOPED: tuple[tuple[str, bool], ...] = (
    ("audiences", True),
    ("campaigns", True),
    ("conversations", True),
    ("knowledge_bases", True),
    ("leads", False),
    ("messages", False),
    ("bookings", False),
    ("ai_runs", False),
    ("whatsapp_templates", False),
    ("notifications", False),
)


def upgrade() -> None:
    # ---------------------------------------------------------------- identity
    op.execute(
        sa.text(
            """
            UPDATE products
               SET slug = :new_slug, display_name = :new_name
             WHERE id = CAST(:id AS uuid)
               AND slug = :placeholder
            """
        ).bindparams(
            id=ACADEMICAE_ID,
            placeholder=PLACEHOLDER_SLUG,
            new_slug=ACADEMICAE_SLUG,
            new_name=ACADEMICAE_NAME,
        )
    )

    # ---------------------------------------------------------------- columns
    for table, has_segment in SCOPED:
        op.add_column(table, sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=True))
        op.create_index(f"ix_{table}_product_id", table, ["product_id"])
        op.create_foreign_key(
            f"fk_{table}_product_id_products",
            table,
            "products",
            ["product_id"],
            ["id"],
            # A product that owns data cannot be deleted out from under it.
            ondelete="RESTRICT",
        )

        if has_segment:
            op.add_column(
                table, sa.Column("segment_id", postgresql.UUID(as_uuid=True), nullable=True)
            )
            op.create_index(f"ix_{table}_segment_id", table, ["segment_id"])
            op.create_foreign_key(
                f"fk_{table}_segment_id_segments",
                table,
                "segments",
                ["segment_id"],
                ["id"],
                ondelete="RESTRICT",
            )
            # A segment must belong to this row's own product (§28.8). MATCH SIMPLE
            # skips the check when either column is NULL, so a product-wide row — and,
            # during this transition, a row with no product yet — is unaffected.
            op.create_foreign_key(
                f"fk_{table}_segment_within_product",
                table,
                "segments",
                ["product_id", "segment_id"],
                ["product_id", "id"],
                ondelete="RESTRICT",
            )

    # ---------------------------------------------------------------- backfill
    #
    # Existing rows are single-product data by definition. No inference, no heuristics.
    for table, _ in SCOPED:
        op.execute(
            sa.text(
                f"UPDATE {table} SET product_id = CAST(:id AS uuid) WHERE product_id IS NULL"  # noqa: S608
            ).bindparams(id=ACADEMICAE_ID)
        )


def downgrade() -> None:
    for table, has_segment in reversed(SCOPED):
        if has_segment:
            op.drop_constraint(f"fk_{table}_segment_within_product", table, type_="foreignkey")
            op.drop_constraint(f"fk_{table}_segment_id_segments", table, type_="foreignkey")
            op.drop_index(f"ix_{table}_segment_id", table_name=table)
            op.drop_column(table, "segment_id")

        op.drop_constraint(f"fk_{table}_product_id_products", table, type_="foreignkey")
        op.drop_index(f"ix_{table}_product_id", table_name=table)
        op.drop_column(table, "product_id")

    # The rename is reversed so a downgrade genuinely returns the previous state.
    op.execute(
        sa.text(
            """
            UPDATE products
               SET slug = :placeholder, display_name = :old_name
             WHERE id = CAST(:id AS uuid)
               AND slug = :new_slug
            """
        ).bindparams(
            id=ACADEMICAE_ID,
            placeholder=PLACEHOLDER_SLUG,
            old_name="Product 1",
            new_slug=ACADEMICAE_SLUG,
        )
    )
