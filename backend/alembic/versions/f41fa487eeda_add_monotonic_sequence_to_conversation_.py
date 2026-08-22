"""Add monotonic sequence to conversation messages

Transcript order decides what the AI reads as the conversation so far, and it could
not be trusted before this. `created_at` defaults to `now()`, which in PostgreSQL is
the *transaction* timestamp: every turn written in one transaction carried an
identical value, so their relative order was whatever the planner returned.

An identity column gives the database the job of ordering them. Backfilling existing
rows is safe because identity assigns values in insert order, which for already-stored
rows is the order they were written in.

Revision ID: f41fa487eeda
Revises: fc345f03b927
Create Date: 2026-08-22 01:42:22.161400
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f41fa487eeda"
down_revision: str | None = "fc345f03b927"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "conversation_messages",
        sa.Column("sequence", sa.BigInteger(), sa.Identity(always=False), nullable=False),
    )
    op.create_index(
        "ix_conversation_messages_conversation_sequence",
        "conversation_messages",
        ["conversation_id", "sequence"],
        unique=False,
    )
    op.create_unique_constraint(
        op.f("uq_conversation_messages_sequence"), "conversation_messages", ["sequence"]
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("uq_conversation_messages_sequence"), "conversation_messages", type_="unique"
    )
    op.drop_index(
        "ix_conversation_messages_conversation_sequence", table_name="conversation_messages"
    )
    op.drop_column("conversation_messages", "sequence")
