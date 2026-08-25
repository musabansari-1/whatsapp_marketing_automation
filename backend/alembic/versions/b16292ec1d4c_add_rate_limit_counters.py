"""Add rate limit counters

§19 requires "appropriate rate limits". This is the storage behind them.

Not in the §15 table list — an implementation mechanism rather than a product
requirement, reported as a deviation rather than added silently, as `sessions` was
for [D2].

Counted in the database rather than in the process because Cloud Run runs several
instances, and an in-process counter would divide the effective limit by the instance
count. For a security control the ceiling has to be absolute.

Revision ID: b16292ec1d4c
Revises: 1a1b00d45fa2
Create Date: 2026-08-25 15:48:11.895525
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b16292ec1d4c"
down_revision: str | None = "1a1b00d45fa2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rate_limit_counters",
        sa.Column("bucket_key", sa.String(length=255), nullable=False),
        sa.Column("count", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rate_limit_counters")),
        sa.UniqueConstraint("bucket_key", name=op.f("uq_rate_limit_counters_bucket_key")),
    )
    op.create_index(
        "ix_rate_limit_counters_expires_at", "rate_limit_counters", ["expires_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_rate_limit_counters_expires_at", table_name="rate_limit_counters")
    op.drop_table("rate_limit_counters")
