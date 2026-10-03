"""Keyword search for hybrid retrieval.

Adds a stored generated `tsvector` column over chunk content plus a GIN index,
so retrieval can match exact terms (names, prices, dates) that dense vectors
underweight. Generated columns backfill existing rows on ADD; the GIN index
builds once. Downgrade drops both — the data is derived, nothing real is lost.

`english` text search configuration: the corpus is English-first with Hindi
terms mixed in. English stemming helps the primary language; other languages
pass through largely unstemmed but still match exactly, which is all a term
match needs. Revisit if a product's corpus goes fully non-English.

Revision ID: a3b4c5d6e7f8
Revises: c2d3e4f5a6b7
Create Date: 2026-10-03
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "a3b4c5d6e7f8"
down_revision = "c2d3e4f5a6b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "ALTER TABLE knowledge_chunks ADD COLUMN IF NOT EXISTS content_tsv tsvector "
            "GENERATED ALWAYS AS (to_tsvector('english', content)) STORED"
        )
    )
    op.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS ix_knowledge_chunks_content_tsv "
            "ON knowledge_chunks USING gin (content_tsv)"
        )
    )


def downgrade() -> None:
    op.execute(sa.text("DROP INDEX IF EXISTS ix_knowledge_chunks_content_tsv"))
    op.execute(sa.text("ALTER TABLE knowledge_chunks DROP COLUMN IF EXISTS content_tsv"))
