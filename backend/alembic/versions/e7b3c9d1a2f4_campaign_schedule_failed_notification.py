"""Bell for a scheduled campaign that did not start.

- ``CAMPAIGN_SCHEDULE_FAILED`` on the ``notification_type`` enum. Same shape as
  ``f2a3b4c5d6e7``: ``ADD VALUE IF NOT EXISTS``, no backfill, a no-op downgrade
  (PostgreSQL cannot drop a label).

Scheduling itself needs no schema change: ``campaigns.scheduled_for`` and the
``SCHEDULED`` campaign status have existed since the first migration.

Revision ID: e7b3c9d1a2f4
Revises: d19b6a4f2c81
Create Date: 2026-11-04
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "e7b3c9d1a2f4"
down_revision = "d19b6a4f2c81"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text("ALTER TYPE notification_type ADD VALUE IF NOT EXISTS 'CAMPAIGN_SCHEDULE_FAILED'")
    )


def downgrade() -> None:
    # The enum label stays: PostgreSQL cannot drop one, and an unused label is harmless.
    pass
