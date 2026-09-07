"""Add products, segments and AI configurations (Phase 17, §28)

The three tables the multi-product architecture rests on. Nothing reads them at this
revision: Phase 18 scopes the existing data to a product, and Phase 22 implements AI
configuration resolution. Establishing the shape first means those phases change
behaviour without also introducing structure.

Product 1 is seeded here rather than by a separate command, because Phase 18's backfill
needs a product to point every existing row at, and a migration that depends on someone
having remembered to run a script is a migration that fails in staging.

Its identity is deliberately fixed rather than generated: Phase 18 must reference this
exact row, and looking it up as "the only product" would break the moment a second one
exists. The slug and display name are **temporary seed values**. Nothing in the
application may branch on them — §28 and CLAUDE.md forbid product-specific code — and
the permanent slug is required before Phase 26, which puts it in `/p/{slug}/` routes.

Revision ID: d3f8b21c60ae
Revises: c7a1e4d90b32
Create Date: 2026-09-07
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "d3f8b21c60ae"
down_revision = "c7a1e4d90b32"
branch_labels = None
depends_on = None

# Seed data, not domain logic. Referenced by Phase 18's backfill and by nothing in
# `app/`. Changing the slug is a data operation until Phase 26 makes it a URL.
PRODUCT_1_ID = "00000000-0000-4000-8000-000000000001"
PRODUCT_1_SLUG = "product-1"
PRODUCT_1_DISPLAY_NAME = "Product 1"


def upgrade() -> None:
    op.create_table(
        "products",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_products")),
        sa.UniqueConstraint("slug", name=op.f("uq_products_slug")),
    )

    op.create_table(
        "segments",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("display_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_segments_product_id_products"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_segments")),
        # Unique within a product, not globally: two products may each have a
        # "government" segment and they are different things.
        sa.UniqueConstraint("product_id", "slug", name="uq_segments_product_slug"),
        # The FK target that keeps an AI configuration's segment inside its own product.
        sa.UniqueConstraint("product_id", "id", name="uq_segments_product_id_id"),
    )
    op.create_index("ix_segments_product_id", "segments", ["product_id"])

    op.create_table(
        "ai_configurations",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=False),
        # NULL identifies the product default; non-NULL is a segment override.
        sa.Column("segment_id", postgresql.UUID(as_uuid=True), nullable=True),
        # Every value column is nullable. NULL means inherit, which is what makes a
        # segment override sparse.
        sa.Column("system_prompt", sa.Text(), nullable=True),
        sa.Column("qualification_required_fields", postgresql.JSONB(), nullable=True),
        sa.Column("model", sa.String(length=255), nullable=True),
        sa.Column("rag_top_k", sa.Integer(), nullable=True),
        sa.Column("rag_min_similarity", sa.Float(), nullable=True),
        sa.Column("rag_max_context_chars", sa.Integer(), nullable=True),
        sa.Column("history_turns", sa.Integer(), nullable=True),
        sa.Column("max_tool_rounds", sa.Integer(), nullable=True),
        sa.Column("fallback_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_ai_configurations_product_id_products"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["segment_id"],
            ["segments.id"],
            name=op.f("fk_ai_configurations_segment_id_segments"),
            ondelete="CASCADE",
        ),
        # A segment override belongs to its own product, enforced as referential
        # integrity. MATCH SIMPLE skips the check when segment_id is NULL, so product
        # defaults are unaffected.
        sa.ForeignKeyConstraint(
            ["product_id", "segment_id"],
            ["segments.product_id", "segments.id"],
            name="fk_ai_configurations_segment_within_product",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_configurations")),
    )

    # Two partial unique indexes, not one UNIQUE (product_id, segment_id).
    #
    # A plain unique constraint would not enforce the first rule at all: SQL treats
    # NULLs as distinct, so a product could accumulate any number of "default" rows and
    # resolution would silently pick one of them. PostgreSQL 15's NULLS NOT DISTINCT
    # would also work, but partial indexes run on every version and each states one rule.
    op.create_index(
        "uq_ai_configurations_product_default",
        "ai_configurations",
        ["product_id"],
        unique=True,
        postgresql_where=sa.text("segment_id IS NULL"),
    )
    op.create_index(
        "uq_ai_configurations_product_segment",
        "ai_configurations",
        ["product_id", "segment_id"],
        unique=True,
        postgresql_where=sa.text("segment_id IS NOT NULL"),
    )

    # ---------------------------------------------------------------- seed
    #
    # Product 1 only. No segments — §28.2 allows a product to have none, and inventing
    # a placeholder to satisfy the schema would put a fiction in the data. No AI
    # configuration row either: Phase 22 decides what an absent row means, and a row
    # copied from environment values now would drift before anything read it.
    op.execute(
        sa.text(
            """
            INSERT INTO products (id, slug, display_name, is_active)
            VALUES (CAST(:id AS uuid), :slug, :display_name, true)
            ON CONFLICT (id) DO NOTHING
            """
        ).bindparams(
            id=PRODUCT_1_ID,
            slug=PRODUCT_1_SLUG,
            display_name=PRODUCT_1_DISPLAY_NAME,
        )
    )


def downgrade() -> None:
    op.drop_index("uq_ai_configurations_product_segment", table_name="ai_configurations")
    op.drop_index("uq_ai_configurations_product_default", table_name="ai_configurations")
    op.drop_table("ai_configurations")
    op.drop_index("ix_segments_product_id", table_name="segments")
    op.drop_table("segments")
    op.drop_table("products")
