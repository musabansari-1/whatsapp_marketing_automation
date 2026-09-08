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
    "leads": (19, "promotion and inbound creation cannot resolve a product yet"),
    "audiences": (19, "POST /audiences/upload takes no product"),
    "campaigns": (19, "inherits from its audience, which has no product yet"),
    "conversations": (22, "Phase 22 snapshots product and segment at creation (§28.5)"),
    "messages": (23, "inherits from campaign or conversation"),
    "ai_runs": (22, "inherits from its conversation"),
    "bookings": (24, "inherits from its conversation; Phase 24 owns per-product calendars"),
    "whatsapp_templates": (23, "template sync has no product context until it is per-product"),
    "knowledge_bases": (26, "POST /knowledge-bases takes no product"),
}

#: Product-scoped, and nullable **by design** rather than as debt.
#:
#: NULL is a meaningful value here and always will be, so no phase closes it.
NULLABLE_BY_DESIGN: dict[str, str] = {
    "notifications": "NULL means a notification that is not about any one product ([D47])",
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

SCOPED = set(NULLABLE_DEBT) | set(NULLABLE_BY_DESIGN)
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

    def test_notifications_is_not_debt(self) -> None:
        """It is nullable by design and no phase should ever tighten it."""
        assert "notifications" not in NULLABLE_DEBT
        assert _table("notifications").columns["product_id"].nullable is True


class TestSegmentScope:
    #: Segment context, always nullable: §28 allows product-wide entities, and a
    #: product may have no segments at all.
    WITH_SEGMENT = ("audiences", "campaigns", "conversations", "knowledge_bases")

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
