"""Template header shape and its media (132012 fix).

Adds `whatsapp_templates.header_type` and `whatsapp_templates.header_media_url`.

**Why.** A campaign send failed permanently on staging with Meta error 132012,
"header: Format mismatch, expected IMAGE, received UNKNOWN": the approved template
carries an IMAGE header, and the send omitted the header component entirely, so Meta
compared the send against a template it did not match. Nothing could retry its way out
— 132012 is permanent by design — so every recipient of that campaign failed.

**`header_type` is schema only; the values are the sync's job.** The column defaults to
NONE, which is correct for every body-only template and therefore for existing rows.
It is *not* a claim that these templates have no header: a template that does have one
keeps reporting NONE until the next WABA-level template sync reads its real shape from
Meta. Backfilling a guess here would be inventing provider metadata, which [D6] forbids
in the same way it forbids inventing an approval status — so the migration writes no
data and the fix requires one sync run after deploying.

**`header_media_url` is deliberately left NULL everywhere.** The asset a media header
carries is application data, and Meta's own record holds only a sample image that is a
placeholder in the manager rather than what a campaign should deliver. Writing the
sample here would send the placeholder to real customers, so an operator sets the real
URL per template instead. Until they do, the template is reported unusable for campaigns
and the send path refuses it by name, which is the honest state.

Revision ID: 7d2e5a91c4f3
Revises: d9a4e2f8c1b0
Create Date: 2026-09-26
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "7d2e5a91c4f3"
down_revision = "d9a4e2f8c1b0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    template_header_type = postgresql.ENUM(
        "NONE", "TEXT", "IMAGE", "VIDEO", "DOCUMENT", name="template_header_type"
    )
    template_header_type.create(op.get_bind(), checkfirst=True)

    op.add_column(
        "whatsapp_templates",
        sa.Column(
            "header_type",
            postgresql.ENUM(
                "NONE", "TEXT", "IMAGE", "VIDEO", "DOCUMENT", name="template_header_type"
            ),
            # NOT NULL with a default, so the existing rows are valid the moment the
            # column exists. NONE is the truthful default for a body-only template and
            # is corrected by the next sync for any that is not.
            nullable=False,
            server_default="NONE",
        ),
    )
    op.add_column(
        "whatsapp_templates",
        # Nullable and never defaulted: a template with no media is a real state, and a
        # placeholder here would be sent to customers.
        sa.Column("header_media_url", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("whatsapp_templates", "header_media_url")
    op.drop_column("whatsapp_templates", "header_type")
    postgresql.ENUM(name="template_header_type").drop(op.get_bind(), checkfirst=True)
