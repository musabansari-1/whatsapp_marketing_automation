"""Add audience membership link (Phase 16, [D55])

Campaign recipient snapshots selected leads with `Lead.source_audience_id ==
campaign.audience_id`. That column is set when a lead is first created and is
deliberately absent from the upsert's ON CONFLICT clause, so re-importing a number
that already existed left it pointing at the *first* audience forever.

The consequence was silent: a campaign on a later audience omitted every lead that
audience shared with an earlier one, reaching fewer people than its audience contained
with no error to notice.

`audience_contacts.lead_id` records which lead each valid contact became, which is the
membership relationship a snapshot should read. A lead can then belong to many
audiences, which is also what the multi-product segment work needs (§28.6).

`source_audience_id` keeps its original meaning — provenance, first seen in — and still
drives the CRM's `wa_lead_source`.

The backfill matches on the normalized phone. That is unambiguous here because
`leads.phone_e164` is still globally unique at this revision; Phase 19 changes that, and
this migration must run before it.

Revision ID: c7a1e4d90b32
Revises: b16292ec1d4c
Create Date: 2026-09-07
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "c7a1e4d90b32"
down_revision = "b16292ec1d4c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "audience_contacts",
        sa.Column("lead_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index("ix_audience_contacts_lead_id", "audience_contacts", ["lead_id"])
    op.create_foreign_key(
        "fk_audience_contacts_lead_id_leads",
        "audience_contacts",
        "leads",
        ["lead_id"],
        ["id"],
        # The contact row is an audit record of what was uploaded (§6) and must
        # outlive the lead it produced.
        ondelete="SET NULL",
    )

    # Backfill membership for audiences already confirmed. VALID rows only: an INVALID
    # row has no usable phone, and a DUPLICATE row repeats a number whose first
    # occurrence is VALID and is already linked.
    op.execute(
        sa.text(
            """
            UPDATE audience_contacts AS ac
               SET lead_id = l.id
              FROM leads AS l
             WHERE ac.phone_e164 = l.phone_e164
               AND ac.validation_status = 'VALID'
               AND ac.lead_id IS NULL
            """
        )
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_audience_contacts_lead_id_leads", "audience_contacts", type_="foreignkey"
    )
    op.drop_index("ix_audience_contacts_lead_id", table_name="audience_contacts")
    op.drop_column("audience_contacts", "lead_id")
