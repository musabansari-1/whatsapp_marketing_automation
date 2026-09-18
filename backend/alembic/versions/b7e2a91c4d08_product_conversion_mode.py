"""Product conversion mode and registration CTA fields.

Not every product converts through calendar booking. Life Herba (and future products)
nudge toward an external registration URL. This revision adds:

- ``products.conversion_mode`` — ``calendar_booking`` (default) or ``external_registration``.
- ``products.registration_url`` — shared destination when mode is external registration.
- ``segments.cta_label`` / ``segments.cta_instruction`` — sparse per-segment CTA copy.

Existing products keep ``calendar_booking`` so Academicae readiness and booking behaviour
are unchanged. Nothing activates a product.

Revision ID: b7e2a91c4d08
Revises: a1b2c3d4e5f6
Create Date: 2026-09-18
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "b7e2a91c4d08"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None

MODE_CHECK = "ck_products_conversion_mode"


def upgrade() -> None:
    op.add_column(
        "products",
        sa.Column(
            "conversion_mode",
            sa.String(length=32),
            nullable=False,
            server_default="calendar_booking",
        ),
    )
    op.create_check_constraint(
        op.f(MODE_CHECK),
        "products",
        "conversion_mode IN ('calendar_booking', 'external_registration')",
    )
    op.add_column("products", sa.Column("registration_url", sa.Text(), nullable=True))
    op.add_column("segments", sa.Column("cta_label", sa.String(length=255), nullable=True))
    op.add_column("segments", sa.Column("cta_instruction", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("segments", "cta_instruction")
    op.drop_column("segments", "cta_label")
    op.drop_column("products", "registration_url")
    op.drop_constraint(op.f(MODE_CHECK), "products", type_="check")
    op.drop_column("products", "conversion_mode")
