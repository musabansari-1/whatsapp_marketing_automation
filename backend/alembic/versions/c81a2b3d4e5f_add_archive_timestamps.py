"""Add archive timestamps to audiences, campaigns and leads.

Revision ID: c81a2b3d4e5f
Revises: f2a3b4c5d6e7
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision: str = "c81a2b3d4e5f"
down_revision: str | None = "f2a3b4c5d6e7"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("audiences", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("campaigns", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("leads", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("leads", "archived_at")
    op.drop_column("campaigns", "archived_at")
    op.drop_column("audiences", "archived_at")
