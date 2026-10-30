"""Record campaign lifecycle activity.

Revision ID: d19b6a4f2c81
Revises: c81a2b3d4e5f
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "d19b6a4f2c81"
down_revision: str | None = "c81a2b3d4e5f"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "campaign_activities",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["campaign_id"], ["campaigns.id"], name="fk_campaign_activities_campaign_id_campaigns", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_campaign_activities"),
    )
    op.create_index(
        "ix_campaign_activities_campaign_created",
        "campaign_activities",
        ["campaign_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_campaign_activities_campaign_created", table_name="campaign_activities")
    op.drop_table("campaign_activities")
