"""Lead-attention bell and the assistant pause behind team replies.

- ``LEAD_NEEDS_ATTENTION`` on the ``notification_type`` enum: a lead asked to
  register or to speak to a person. Same shape as ``c2d3e4f5a6b7``: ``ADD VALUE
  IF NOT EXISTS``, no backfill, a no-op downgrade (PostgreSQL cannot drop a label).
- ``conversations.ai_paused_until``: set when a team member replies from the
  dashboard, so the assistant does not answer over them. NULL means not paused.

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-10-23
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "f2a3b4c5d6e7"
down_revision = "e1f2a3b4c5d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text("ALTER TYPE notification_type ADD VALUE IF NOT EXISTS 'LEAD_NEEDS_ATTENTION'")
    )
    op.add_column(
        "conversations",
        sa.Column("ai_paused_until", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("conversations", "ai_paused_until")
    # The enum label stays: PostgreSQL cannot drop one, and an unused label is harmless.
