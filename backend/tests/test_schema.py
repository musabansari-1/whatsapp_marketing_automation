"""Schema shape tests.

These read the SQLAlchemy metadata rather than a live database, so they run
everywhere and fail fast when a constraint the specification requires is dropped by
accident. The behaviour those constraints produce is verified against real
PostgreSQL in tests/integration/.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Table, UniqueConstraint

from app.persistence.base import Base
from app.persistence.models import EMBEDDING_DIMENSION

# Specification §15 lists 18 tables; [D16] adds whatsapp_suppressions, and [D2]'s
# server-side sessions add `sessions` (a documented deviation from the §15 list).
EXPECTED_TABLES = {
    "users",
    "sessions",
    "audiences",
    "audience_contacts",
    "leads",
    "campaigns",
    "campaign_recipients",
    "whatsapp_templates",
    "whatsapp_suppressions",
    "messages",
    "conversations",
    "conversation_messages",
    "bookings",
    "notifications",
    "webhook_events",
    "ai_runs",
    "knowledge_bases",
    "knowledge_documents",
    "knowledge_chunks",
    "knowledge_ingestion_jobs",
}


def _table(name: str) -> Table:
    return Base.metadata.tables[name]


def _unique_column_sets(table: Table) -> set[frozenset[str]]:
    """Unique constraints and unique indexes, as column-name sets."""
    sets: set[frozenset[str]] = set()
    for constraint in table.constraints:
        if isinstance(constraint, UniqueConstraint):
            sets.add(frozenset(c.name for c in constraint.columns))
    for index in table.indexes:
        if index.unique:
            sets.add(frozenset(c.name for c in index.columns))
    return sets


def _index_names(table: Table) -> set[str]:
    return {index.name for index in table.indexes if index.name}


class TestTables:
    def test_all_specified_tables_exist(self) -> None:
        assert set(Base.metadata.tables) == EXPECTED_TABLES

    def test_table_count(self) -> None:
        assert len(Base.metadata.tables) == 20


class TestIdentityAndDeduplication:
    def test_lead_phone_is_unique(self) -> None:
        """[D11]: normalized phone is the primary deduplication identity."""
        assert frozenset({"phone_e164"}) in _unique_column_sets(_table("leads"))

    def test_suppression_phone_is_unique(self) -> None:
        """[D16] names this constraint explicitly."""
        assert frozenset({"phone_e164"}) in _unique_column_sets(_table("whatsapp_suppressions"))

    def test_suppression_is_a_separate_table_from_leads(self) -> None:
        """[D16]: opt-out must survive independently of any lead row."""
        suppressions = _table("whatsapp_suppressions")
        lead_fk = next(
            (fk for c in suppressions.columns for fk in c.foreign_keys),
            None,
        )
        assert lead_fk is not None
        # Nullable and SET NULL: deleting the lead must not delete the suppression.
        assert suppressions.c.lead_id.nullable is True
        assert lead_fk.ondelete == "SET NULL"

    def test_lead_opted_out_flag_exists_but_is_not_the_only_signal(self) -> None:
        assert "opted_out" in _table("leads").c
        assert "whatsapp_suppressions" in Base.metadata.tables


class TestIdempotencyConstraints:
    """§20 requires idempotency; each of these is where it is enforced."""

    def test_campaign_recipient_pair_is_unique(self) -> None:
        assert frozenset({"campaign_id", "lead_id"}) in _unique_column_sets(
            _table("campaign_recipients")
        )

    def test_provider_message_id_is_unique(self) -> None:
        """§15: unique constraints for provider IDs."""
        assert frozenset({"provider_message_id"}) in _unique_column_sets(_table("messages"))

    def test_message_idempotency_key_is_unique(self) -> None:
        assert frozenset({"idempotency_key"}) in _unique_column_sets(_table("messages"))

    def test_webhook_event_id_is_unique(self) -> None:
        """§8: duplicate-event protection."""
        assert frozenset({"provider_event_id"}) in _unique_column_sets(_table("webhook_events"))

    def test_calendar_event_id_is_unique(self) -> None:
        """§12: a retried booking must not create a second calendar event."""
        assert frozenset({"calendar_event_id"}) in _unique_column_sets(_table("bookings"))

    def test_notification_fanout_is_idempotent_per_recipient(self) -> None:
        """[D3] fans out per user, so retries must not duplicate rows."""
        assert frozenset(
            {"user_id", "type", "related_entity_type", "related_entity_id"}
        ) in _unique_column_sets(_table("notifications"))

    def test_chunk_index_is_unique_per_document(self) -> None:
        assert frozenset({"document_id", "chunk_index"}) in _unique_column_sets(
            _table("knowledge_chunks")
        )

    def test_ingestion_task_id_is_unique(self) -> None:
        assert frozenset({"task_id"}) in _unique_column_sets(_table("knowledge_ingestion_jobs"))


class TestAudienceStoresInvalidRows:
    def test_no_unique_phone_constraint_on_audience_contacts(self) -> None:
        """§6 requires storing duplicate and invalid rows, so uniqueness here would
        make the pipeline unable to record what it rejected."""
        unique_sets = _unique_column_sets(_table("audience_contacts"))
        assert frozenset({"phone_e164"}) not in unique_sets
        assert frozenset({"audience_id", "phone_e164"}) not in unique_sets

    def test_validation_fields_present(self) -> None:
        columns = _table("audience_contacts").c
        for field in ("row_number", "validation_status", "validation_error", "raw_row"):
            assert field in columns


class TestBookings:
    def test_specified_fields_present(self) -> None:
        """§15 lists the booking fields."""
        columns = _table("bookings").c
        for field in (
            "lead_id",
            "conversation_id",
            "calendar_event_id",
            "start_time",
            "end_time",
            "timezone",
            "status",
            "created_at",
            "updated_at",
        ):
            assert field in columns

    def test_end_must_follow_start(self) -> None:
        names = {c.name for c in _table("bookings").constraints if c.name}
        assert "ck_bookings_end_after_start" in names

    def test_confirmed_slot_is_uniquely_held(self) -> None:
        """§12 race handling: two confirmations for one slot cannot both commit."""
        index = next(
            i for i in _table("bookings").indexes if i.name == "uq_bookings_confirmed_start_time"
        )
        assert index.unique is True
        # Partial, so cancelled bookings do not block rebooking the slot.
        assert index.dialect_options["postgresql"]["where"] is not None

    def test_timezone_is_stored_per_booking(self) -> None:
        """[D8]: a later policy change must not reinterpret an existing booking."""
        assert _table("bookings").c.timezone.nullable is False


class TestVectorColumn:
    def test_dimension_follows_configuration(self) -> None:
        """[D1]: never a hard-coded literal."""
        from app.core.config import get_settings

        assert get_settings().embedding_dimension == EMBEDDING_DIMENSION

    def test_initial_dimension_is_384(self) -> None:
        """all-MiniLM-L6-v2 ([D1])."""
        assert EMBEDDING_DIMENSION == 384

    def test_embedding_column_uses_the_configured_dimension(self) -> None:
        column = _table("knowledge_chunks").c.embedding
        assert column.type.dim == EMBEDDING_DIMENSION  # type: ignore[attr-defined]

    def test_vectors_are_attributable_to_a_model(self) -> None:
        """[D1] forbids mixing vectors from incompatible models."""
        assert "embedding_model" in _table("knowledge_chunks").c

    def test_similarity_index_exists(self) -> None:
        """§15 requires an index for vector similarity."""
        index = next(
            i
            for i in _table("knowledge_chunks").indexes
            if i.name == "ix_knowledge_chunks_embedding_hnsw"
        )
        assert index.dialect_options["postgresql"]["using"] == "hnsw"


class TestLookupIndexes:
    """§15: indexes for lead/campaign/booking lookups and notifications."""

    @pytest.mark.parametrize(
        ("table_name", "index_name"),
        [
            ("leads", "ix_leads_status"),
            ("campaigns", "ix_campaigns_status"),
            ("campaign_recipients", "ix_campaign_recipients_campaign_status"),
            ("bookings", "ix_bookings_lead_id"),
            ("bookings", "ix_bookings_start_time"),
            ("notifications", "ix_notifications_user_unread"),
            ("messages", "ix_messages_campaign_id"),
            ("conversations", "ix_conversations_lead_id"),
            ("knowledge_documents", "ix_knowledge_documents_kb_status"),
            ("users", "ix_users_is_active"),
        ],
    )
    def test_index_present(self, table_name: str, index_name: str) -> None:
        assert index_name in _index_names(_table(table_name))


class TestQualificationData:
    def test_leads_carry_qualification_jsonb(self) -> None:
        """[D18]: AI-extracted facts land here, not in `status`."""
        assert "qualification_data" in _table("leads").c
        assert _table("leads").c.qualification_data.nullable is False


class TestTimestamps:
    def test_all_timestamps_are_timezone_aware(self) -> None:
        """§12 depends on correct timezone handling; naive columns are how that
        silently breaks."""
        offenders = [
            f"{table.name}.{column.name}"
            for table in Base.metadata.tables.values()
            for column in table.columns
            if column.type.__class__.__name__ == "DateTime" and not column.type.timezone  # type: ignore[attr-defined]
        ]
        assert offenders == []
