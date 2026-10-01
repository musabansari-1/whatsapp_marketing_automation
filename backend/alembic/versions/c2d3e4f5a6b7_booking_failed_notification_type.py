"""Failed-booking notification type.

Adds `BOOKING_FAILED` to the `notification_type` enum for the bell a terminal
booking failure rings. Plain `op.execute` with `IF NOT EXISTS`, no backfill: no
row can carry the value yet.

Same PostgreSQL shape as `f1a2b3c4d5e6` (BOT_SUSPECTED): `ALTER TYPE ... ADD
VALUE` runs inside the migration transaction on PostgreSQL 12+, and the
downgrade is a deliberate no-op — PostgreSQL cannot drop a label, a lingering
unused one is harmless, and `IF NOT EXISTS` keeps the CI upgrade → downgrade →
upgrade cycle green across that no-op.

Revision ID: c2d3e4f5a6b7
Revises: f1a2b3c4d5e6
Create Date: 2026-10-01
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "c2d3e4f5a6b7"
down_revision = "f1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("ALTER TYPE notification_type ADD VALUE IF NOT EXISTS 'BOOKING_FAILED'"))


def downgrade() -> None:
    # PostgreSQL cannot drop an enum value. Left in place deliberately: see above.
    pass
