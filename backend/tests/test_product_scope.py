"""Product scope, and the nullability debt it is carrying (Phase 18, §28).

Every table in the schema is classified here, and the classification is asserted rather
than described. A table added later cannot quietly become product-blind: it will not
appear in any category and this fails.

The point of the debt table is that a nullable `product_id` is *tracked*, not
accidental. Phase 18 could not make these columns NOT NULL without breaking the write
paths that feed them — `POST /audiences/upload` and `POST /knowledge-bases` take no
product, template sync has none, and inbound webhooks cannot route until Phase 23 — so
each column carries the phase that closes it. A column with no closing phase is a bug.

Model-level, so it runs without a database and stays fast.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Table

from app.persistence.base import Base

# Importing the models package is what registers every table on the metadata this
# module inspects. Without it the schema reads as empty and every assertion here
# passes or fails for the wrong reason.
import app.persistence.models  # noqa: F401  # isort: skip

# ---------------------------------------------------------------- classification

#: Product-owned, and the phase that will make `product_id` NOT NULL.
#:
#: Nullable today because the write path cannot yet supply a product. The phase named is
#: the one that teaches it to — after which that phase tightens the column.
NULLABLE_DEBT: dict[str, tuple[int, str]] = {
    # Closed in Phase 19: audience upload resolves a product, and a campaign takes its
    # product from its audience. `audiences` and `campaigns` are no longer here.
    #
    # `leads` was originally listed against Phase 19 and that was wrong: promotion can
    # supply a product from the audience, but the **inbound** path cannot until routing
    # on `metadata.phone_number_id` arrives, and [D45] forbids guessing one. Phase 23
    # closes it, after attributing or refusing any transitional rows.
    "knowledge_bases": (26, "POST /knowledge-bases takes no product"),
}

#: Product-scoped, and nullable **by design** rather than as debt.
#:
#: NULL is a meaningful value here and always will be, so no phase closes it.
NULLABLE_BY_DESIGN: dict[str, str] = {
    "notifications": "NULL means a notification that is not about any one product ([D47])",
    # Was debt after Phase 23, and reclassified by decision rather than by a phase ([D57]).
    #
    # One Meta App and one shared WABA give one template pool, and Meta records no owner
    # on a template. A newly discovered one therefore genuinely has no product until
    # someone assigns it — NULL is that state, not a gap waiting to be filled. What must
    # hold is not "every template has a product" but "every template a campaign uses is
    # owned by that campaign's product", which the campaign rules enforce.
    "whatsapp_templates": (
        "NULL means a shared-WABA template not yet assigned to a product; no campaign may use it"
    ),
}

#: `segment_id` columns, all nullable by design (§28.2).
#:
#: A product may have no segments at all, and product-wide entities are ordinary.
SEGMENT_NULLABLE_BY_DESIGN: dict[str, str] = {
    "audiences": "NULL means an audience targeting the whole product",
    "campaigns": "NULL means a product-wide campaign, taken from its audience",
    "conversations": "NULL means no segment could be proven at creation; never guessed ([D45])",
    "knowledge_bases": "NULL means shared across every segment of the product ([D56])",
}

#: Global by design. Not product-scoped, and must never become so.
INTENTIONALLY_GLOBAL: dict[str, str] = {
    "users": "one internal team across every product ([D46])",
    "sessions": "authentication is per person, never per product",
    "rate_limit_counters": "infrastructure; the limits protect the deployment (§19)",
    "webhook_events": "a raw provider log, stored before anything is parsed",
    "whatsapp_suppressions": "opt-out is global at the phone level across all products ([D42])",
    "products": "the root of the scope; it cannot be scoped to itself",
    "segments": "scoped by `product_id` as its owner, not as a scope column",
    "ai_configurations": "scoped by `product_id` as its owner, not as a scope column",
}

#: Scope is inherited through a foreign key, so no column of their own.
DERIVED: dict[str, str] = {
    "audience_contacts": "-> audiences",
    "campaign_recipients": "-> campaigns",
    "conversation_messages": "-> conversations",
    "knowledge_documents": "-> knowledge_bases",
    "knowledge_chunks": "-> knowledge_documents -> knowledge_bases",
    "knowledge_ingestion_jobs": "-> knowledge_documents",
}

#: Product-owned and already NOT NULL. Their write paths resolve a product.
CLOSED: dict[str, str] = {
    "audiences": "closed in Phase 19; upload resolves a product explicitly or by sole-active",
    "campaigns": "closed in Phase 19; taken from the campaign's audience (§28.6)",
    "leads": "closed in Phase 23; inbound routing resolves the product ([D40])",
    "conversations": "closed in Phase 23; snapshotted from the lead (§28.5)",
    "messages": "closed in Phase 23; from the campaign or the conversation",
    "ai_runs": "closed in Phase 23; from the conversation",
    "bookings": "closed in Phase 24; from the conversation, whose product's calendar confirmed it",
}

SCOPED = set(NULLABLE_DEBT) | set(NULLABLE_BY_DESIGN) | set(CLOSED)
ALL_CLASSIFIED = SCOPED | set(INTENTIONALLY_GLOBAL) | set(DERIVED)


def _table(name: str) -> Table:
    return Base.metadata.tables[name]


class TestEveryTableIsClassified:
    def test_no_table_is_unclassified(self) -> None:
        """The guard that makes the rest lasting.

        A table added in a later phase must be placed in one of the four categories
        above. Until it is, this fails — which is the only reliable way to stop a table
        becoming product-blind by omission.
        """
        assert set(Base.metadata.tables) == ALL_CLASSIFIED

    def test_no_table_is_in_two_categories(self) -> None:
        groups = [
            set(NULLABLE_DEBT),
            set(NULLABLE_BY_DESIGN),
            set(CLOSED),
            set(INTENTIONALLY_GLOBAL),
            set(DERIVED),
        ]
        seen: set[str] = set()
        for group in groups:
            assert not (seen & group), f"classified twice: {seen & group}"
            seen |= group


class TestScopedTables:
    @pytest.mark.parametrize("name", sorted(SCOPED))
    def test_has_a_product_column(self, name: str) -> None:
        assert "product_id" in _table(name).columns

    @pytest.mark.parametrize("name", sorted(SCOPED))
    def test_the_product_column_references_products(self, name: str) -> None:
        targets = {fk.column.table.name for fk in _table(name).columns["product_id"].foreign_keys}
        assert "products" in targets

    @pytest.mark.parametrize("name", sorted(SCOPED))
    def test_the_product_column_is_indexed(self, name: str) -> None:
        """Every future scoped query filters on it."""
        indexed = {tuple(column.name for column in index.columns) for index in _table(name).indexes}
        assert any(columns and columns[0] == "product_id" for columns in indexed)


class TestNullabilityDebt:
    """Nullable today, and each one owned by a phase that closes it."""

    @pytest.mark.parametrize("name", sorted(NULLABLE_DEBT))
    def test_is_still_nullable(self, name: str) -> None:
        """Fails when a phase tightens a column without updating this table.

        That is deliberate: the debt list is only honest if closing an entry is a
        visible act rather than something noticed months later.
        """
        assert _table(name).columns["product_id"].nullable is True

    @pytest.mark.parametrize(("name", "entry"), sorted(NULLABLE_DEBT.items()))
    def test_names_a_closing_phase_and_a_reason(self, name: str, entry: tuple[int, str]) -> None:
        phase, reason = entry
        assert 19 <= phase <= 27, f"{name}: closing phase {phase} is not a real phase"
        assert reason.strip(), f"{name}: no reason recorded"

    @pytest.mark.parametrize("name", sorted(CLOSED))
    def test_a_closed_column_is_not_null(self, name: str) -> None:
        """Debt that has been paid must show as paid.

        A table listed as closed while its column is still nullable would be a register
        that flatters itself, which is worse than no register.
        """
        assert _table(name).columns["product_id"].nullable is False

    def test_the_inbound_chain_closed_together(self) -> None:
        """Leads, conversations, messages and AI runs shared one blocker, and Phase 23
        removed it for all of them at once.

        Recorded because the plan first split them across 19, 22 and 23: a conversation
        takes its product from its lead and a run from its conversation, so none could
        close before inbound routing determined the lead's ([D40], [D45]).
        """
        for table in ("leads", "conversations", "messages", "ai_runs"):
            assert table in CLOSED, f"{table} should have closed with inbound routing"
            assert table not in NULLABLE_DEBT

    @pytest.mark.parametrize("name", sorted(NULLABLE_BY_DESIGN))
    def test_a_by_design_column_is_nullable_and_not_debt(self, name: str) -> None:
        """No phase should ever tighten these, so none may appear in the debt list."""
        assert name not in NULLABLE_DEBT
        assert _table(name).columns["product_id"].nullable is True

    def test_templates_are_by_design_not_debt(self) -> None:
        """[D57], recorded on its own because Phase 23 first listed it as debt.

        A shared WABA returns one template pool with no owner on any template, so an
        unassigned template is a real state. Scheduling the column to become NOT NULL
        would mean either inventing owners or refusing to store what Meta reports.
        """
        assert "whatsapp_templates" in NULLABLE_BY_DESIGN
        assert "whatsapp_templates" not in NULLABLE_DEBT
        assert "whatsapp_templates" not in CLOSED

    def test_assigned_templates_are_unique_per_product(self) -> None:
        """The application invariant for owned templates: within one product, a name and
        language identify one template. Partial, because unassigned provider artifacts
        are not an application namespace."""
        index = next(
            (
                index
                for index in _table("whatsapp_templates").indexes
                if index.name == "uq_whatsapp_templates_product_name_language"
            ),
            None,
        )
        assert index is not None
        assert index.unique
        assert [column.name for column in index.columns] == ["product_id", "name", "language"]
        assert "product_id IS NOT NULL" in str(index.dialect_options["postgresql"]["where"])

    def test_provider_identity_is_the_provider_template_id(self) -> None:
        """One Meta artifact is one row. Ownership is not part of its identity, so the
        same template is never copied into a row per product."""
        assert _table("whatsapp_templates").columns["provider_template_id"].unique is True


class TestSegmentScope:
    #: Segment context, always nullable: §28 allows product-wide entities, and a
    #: product may have no segments at all.
    WITH_SEGMENT = tuple(SEGMENT_NULLABLE_BY_DESIGN)

    @pytest.mark.parametrize("name", WITH_SEGMENT)
    def test_segment_is_nullable(self, name: str) -> None:
        assert _table(name).columns["segment_id"].nullable is True

    @pytest.mark.parametrize("name", WITH_SEGMENT)
    def test_a_segment_cannot_cross_products(self, name: str) -> None:
        """§28.8 as referential integrity: a composite key into segments (product, id).

        Without it, a row could pair one product with another product's segment and
        only an application check would stand in the way.
        """
        composite = [
            constraint
            for constraint in _table(name).foreign_key_constraints
            if {column.name for column in constraint.columns} == {"product_id", "segment_id"}
        ]
        assert composite, f"{name}: no composite (product_id, segment_id) foreign key"
        assert composite[0].referred_table.name == "segments"

    def test_leads_has_no_segment_column(self) -> None:
        """[D48]. A lead may belong to several segments within a product, so a single
        column would be misleading, would duplicate membership state, and would
        eventually be mistaken for authority.

        Membership is `audience_contacts` -> `audiences.segment_id`. The operative
        segment for AI behaviour lives on the conversation snapshot.
        """
        assert "segment_id" not in _table("leads").columns

    def test_only_the_approved_tables_carry_a_segment(self) -> None:
        carrying = {
            name
            for name, table in Base.metadata.tables.items()
            if "segment_id" in table.columns and name != "ai_configurations"
        }
        assert carrying == set(self.WITH_SEGMENT)


class TestGlobalTablesStayGlobal:
    @pytest.mark.parametrize("name", sorted(INTENTIONALLY_GLOBAL))
    def test_has_no_scope_column(self, name: str) -> None:
        """`products`, `segments` and `ai_configurations` own a `product_id` as their
        parent reference rather than as a scope column, so they are checked by name."""
        if name in {"segments", "ai_configurations"}:
            assert "product_id" in _table(name).columns
            return
        assert "product_id" not in _table(name).columns

    def test_suppression_is_global(self) -> None:
        """[D42] is the one worth asserting on its own.

        Opt-out stays global at the phone level across every product. A `product_id`
        appearing here would mean product-scoped consent, which is explicitly not
        implemented.
        """
        assert "product_id" not in _table("whatsapp_suppressions").columns


class TestDerivedTablesInheritScope:
    @pytest.mark.parametrize("name", sorted(DERIVED))
    def test_has_no_product_column(self, name: str) -> None:
        """Their scope comes through a foreign key. A column here would be a second
        source of truth, free to drift from the first."""
        assert "product_id" not in _table(name).columns
