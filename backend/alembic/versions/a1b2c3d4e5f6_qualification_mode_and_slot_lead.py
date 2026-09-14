"""AI judgement qualification mode, booking min-lead, and Academicae inactive by default.

Adds:
- `ai_configurations.qualification_mode` — NULL inherits; `required_fields` or
  `ai_judgement` when set ([D37] Academicae launch).
- `products.demo_min_lead_days` — NULL inherits the platform default (0). A product
  may set 1 so the earliest offerable day is tomorrow.

`products.demo_booking_horizon_days` already existed. The application now treats the
sentinel value `-1` as an unbounded horizon (catalog key `"unbounded"`). NULL on that
column continues to mean inherit — the two must not be collapsed.

**Academicae inactive by default.** The Phase 17 migration seeds product
`00000000-0000-4000-8000-000000000001` active and is left unchanged (already shipped).
This revision sets that row inactive so a migrate → configure-products sequence cannot
leave Academicae live until `activate-product academicae` is run explicitly. Downgrade
does not re-activate it.

Revision ID: a1b2c3d4e5f6
Revises: f4a1c7e9b2d5
Create Date: 2026-09-14
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "a1b2c3d4e5f6"
down_revision = "f4a1c7e9b2d5"
branch_labels = None
depends_on = None

MODE_CHECK = "ck_ai_configurations_qualification_mode"
ACADEMICAE_ID = "00000000-0000-4000-8000-000000000001"


def upgrade() -> None:
    op.add_column(
        "ai_configurations",
        sa.Column("qualification_mode", sa.String(length=32), nullable=True),
    )
    op.create_check_constraint(
        op.f(MODE_CHECK),
        "ai_configurations",
        "qualification_mode IS NULL OR qualification_mode IN ('required_fields', 'ai_judgement')",
    )
    op.add_column(
        "products",
        sa.Column("demo_min_lead_days", sa.Integer(), nullable=True),
    )

    # Idempotent. Safe if the row is already inactive or absent (empty database).
    op.execute(
        sa.text(
            """
            UPDATE products
            SET is_active = false
            WHERE id = CAST(:id AS uuid) AND is_active = true
            """
        ).bindparams(id=ACADEMICAE_ID)
    )


def downgrade() -> None:
    """Drop the new columns only.

    Does **not** set Academicae active again. Re-activating on downgrade would undo the
    safety this revision exists for; activation stays an explicit `activate-product`.
    """
    op.drop_column("products", "demo_min_lead_days")
    op.drop_constraint(op.f(MODE_CHECK), "ai_configurations", type_="check")
    op.drop_column("ai_configurations", "qualification_mode")
