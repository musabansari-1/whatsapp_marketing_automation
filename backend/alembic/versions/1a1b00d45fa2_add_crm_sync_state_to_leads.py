"""Add CRM sync state to leads

§14 makes HubSpot an integration rather than the database, so these columns record
only what the platform needs to keep synchronization idempotent and visible: which
contact a lead maps to, when it last synced, and why it last failed.

Nullable throughout, and no unique constraint on `crm_contact_id`. HubSpot can
legitimately match two platform leads to one contact — its own identity is
email-based while ours is the phone number ([D11]) — and a unique constraint would
turn that into a failed sync rather than a recorded link.

Revision ID: 1a1b00d45fa2
Revises: f41fa487eeda
Create Date: 2026-08-23 18:50:32.564763
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "1a1b00d45fa2"
down_revision: str | None = "f41fa487eeda"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("leads", sa.Column("crm_contact_id", sa.String(length=64), nullable=True))
    op.add_column("leads", sa.Column("crm_synced_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("leads", sa.Column("crm_sync_error", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("leads", "crm_sync_error")
    op.drop_column("leads", "crm_synced_at")
    op.drop_column("leads", "crm_contact_id")
