"""Bot-suspect notification type ([D67]).

Adds `BOT_SUSPECTED` to the `notification_type` enum for the hourly bot-activity
audit's flags. Plain `op.execute` with `IF NOT EXISTS`, no backfill: no row can
carry the value yet.

Two PostgreSQL facts shape this file:

- `ALTER TYPE ... ADD VALUE` runs inside a migration transaction on PostgreSQL
  12+, which covers every database this platform runs against (pgvector:pg16
  locally and in CI, Neon in staging/production). No autocommit juggling is
  needed; the migration-path test proves the whole cycle on PG16.
- PostgreSQL cannot drop an enum value, so the downgrade is deliberately a
  no-op: a lingering unused label is harmless, while pretending to remove it
  would lie about what the database holds. `IF NOT EXISTS` keeps the CI
  upgrade → downgrade → upgrade cycle green across that no-op.

Revision ID: f1a2b3c4d5e6
Revises: e8f9a0b1c2d3
Create Date: 2026-09-29
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "f1a2b3c4d5e6"
down_revision = "e8f9a0b1c2d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("ALTER TYPE notification_type ADD VALUE IF NOT EXISTS 'BOT_SUSPECTED'"))


def downgrade() -> None:
    # PostgreSQL cannot drop an enum value. Left in place deliberately: see above.
    pass
