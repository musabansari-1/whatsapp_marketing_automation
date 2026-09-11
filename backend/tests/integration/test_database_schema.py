"""Schema behaviour against real PostgreSQL.

Verifies what only a real database can: that constraints actually reject the writes
they are meant to, that enums round-trip, and that pgvector similarity search works.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import (
    BookingStatus,
    ContactValidationStatus,
    LeadStatus,
    MessageDirection,
    NotificationType,
    UserRole,
)
from app.persistence.models import (
    Audience,
    AudienceContact,
    Booking,
    KnowledgeBase,
    KnowledgeChunk,
    KnowledgeDocument,
    Lead,
    Message,
    Notification,
    User,
    WebhookEvent,
    WhatsAppSuppression,
)
from tests.integration.helpers import ensure_product_id

pytestmark = pytest.mark.integration

PHONE = "+919876543210"


async def _make_lead(session: AsyncSession, phone: str = PHONE) -> Lead:
    lead = Lead(phone_e164=phone, product_id=await ensure_product_id(session))
    session.add(lead)
    await session.flush()
    return lead


async def _make_user(session: AsyncSession, email: str = "admin@example.com") -> User:
    # noqa S106: a placeholder hash for a schema test, not a credential. Real
    # password hashing arrives with authentication in Phase 3.
    user = User(email=email, password_hash="not-a-real-hash", role=UserRole.ADMIN)  # noqa: S106
    session.add(user)
    await session.flush()
    return user


class TestExtensionAndTables:
    async def test_pgvector_is_installed(self, session: AsyncSession) -> None:
        result = await session.execute(
            text("SELECT extname FROM pg_extension WHERE extname='vector'")
        )
        assert result.scalar_one() == "vector"

    async def test_all_tables_exist(self, session: AsyncSession) -> None:
        """Twenty-one against a real database: §15's eighteen, plus
        `whatsapp_suppressions` ([D16]), `sessions` ([D2]) and `rate_limit_counters`
        (§19).

        The companion in `tests/test_schema.py` asserts the same count against the
        models. Both exist because they catch different mistakes: that one catches a
        model nobody migrated, this one a migration that did not run.
        """
        result = await session.execute(
            text(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema='public' AND table_name <> 'alembic_version'"
            )
        )
        assert result.scalar_one() == 24


class TestDefaultsAndEnums:
    async def test_uuid_and_timestamps_are_server_generated(self, session: AsyncSession) -> None:
        lead = await _make_lead(session)
        assert isinstance(lead.id, uuid.UUID)
        assert lead.created_at.tzinfo is not None

    async def test_enum_round_trips(self, session: AsyncSession) -> None:
        lead = await _make_lead(session)
        assert lead.status is LeadStatus.NEW

        lead.status = LeadStatus.QUALIFIED
        await session.flush()
        await session.refresh(lead)

        assert lead.status is LeadStatus.QUALIFIED

    async def test_invalid_enum_value_is_rejected(self, session: AsyncSession) -> None:
        """The database enforces the state set, not just the application."""
        with pytest.raises(Exception):  # noqa: B017 - driver-specific DataError
            await session.execute(
                text("INSERT INTO leads (phone_e164, status) VALUES ('+911111111111', 'BOGUS')")
            )

    async def test_qualification_data_defaults_to_empty_object(self, session: AsyncSession) -> None:
        lead = await _make_lead(session)
        assert lead.qualification_data == {}

    async def test_qualification_data_stores_structured_facts(self, session: AsyncSession) -> None:
        """[D18]: the AI writes here; only the backend sets `status`."""
        lead = await _make_lead(session)
        lead.qualification_data = {"company_size": 50, "budget_confirmed": True}
        await session.flush()
        await session.refresh(lead)

        assert lead.qualification_data["company_size"] == 50
        assert lead.qualification_data["budget_confirmed"] is True


class TestUniqueConstraints:
    async def test_lead_phone_is_unique_within_a_product(self, session: AsyncSession) -> None:
        """[D11] as [D41] refined it: one lead per phone *per product*.

        Both rows here name the same product, which is what makes them the same
        person in the same pipeline. The same phone under a *different* product is a
        different lead and is allowed — `test_product_routing.py` covers that side.
        """
        lead = await _make_lead(session)
        session.add(Lead(phone_e164=PHONE, product_id=lead.product_id))
        with pytest.raises(IntegrityError):
            await session.flush()

    async def test_suppression_phone_is_unique(self, session: AsyncSession) -> None:
        """[D16] names this constraint explicitly."""
        session.add(WhatsAppSuppression(phone_e164=PHONE))
        await session.flush()
        session.add(WhatsAppSuppression(phone_e164=PHONE))
        with pytest.raises(IntegrityError):
            await session.flush()

    async def test_provider_message_id_is_unique(self, session: AsyncSession) -> None:
        for _ in range(2):
            session.add(
                Message(
                    direction=MessageDirection.OUTBOUND,
                    phone_e164=PHONE,
                    provider_message_id="wamid.SAME",
                    product_id=await ensure_product_id(session),
                )
            )
        with pytest.raises(IntegrityError):
            await session.flush()

    async def test_webhook_event_id_is_unique(self, session: AsyncSession) -> None:
        """§8 duplicate-event protection: Meta redelivers."""
        for _ in range(2):
            session.add(WebhookEvent(provider_event_id="evt-1", event_type="messages", payload={}))
        with pytest.raises(IntegrityError):
            await session.flush()

    async def test_calendar_event_id_is_unique(self, session: AsyncSession) -> None:
        """§12: a retried booking task must not produce a second calendar event."""
        lead = await _make_lead(session)
        start = datetime(2026, 9, 1, 10, tzinfo=UTC)
        for _ in range(2):
            session.add(
                Booking(
                    product_id=lead.product_id,
                    lead_id=lead.id,
                    calendar_event_id="evt-same",
                    start_time=start,
                    end_time=start + timedelta(minutes=30),
                    timezone="Asia/Kolkata",
                )
            )
        with pytest.raises(IntegrityError):
            await session.flush()


class TestSuppressionOutlivesLead:
    async def test_deleting_a_lead_leaves_the_suppression(self, session: AsyncSession) -> None:
        """[D16]: opt-out must survive independently of any lead row.

        This is the whole reason suppression is its own table.
        """
        lead = await _make_lead(session)
        session.add(WhatsAppSuppression(phone_e164=PHONE, lead_id=lead.id))
        await session.flush()

        await session.delete(lead)
        await session.flush()

        remaining = (
            await session.execute(
                select(WhatsAppSuppression).where(WhatsAppSuppression.phone_e164 == PHONE)
            )
        ).scalar_one()
        assert remaining.phone_e164 == PHONE
        assert remaining.lead_id is None


class TestBookingConstraints:
    async def test_end_must_follow_start(self, session: AsyncSession) -> None:
        lead = await _make_lead(session)
        start = datetime(2026, 9, 1, 10, tzinfo=UTC)
        session.add(
            Booking(
                product_id=lead.product_id,
                lead_id=lead.id,
                start_time=start,
                end_time=start,
                timezone="Asia/Kolkata",
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()

    async def test_two_confirmed_bookings_cannot_share_a_slot(self, session: AsyncSession) -> None:
        """§12 race handling, enforced by the database rather than only by recheck."""
        first = await _make_lead(session)
        second = await _make_lead(session, "+919000000001")
        start = datetime(2026, 9, 1, 10, tzinfo=UTC)

        for lead, event_id in ((first, "evt-1"), (second, "evt-2")):
            session.add(
                Booking(
                    product_id=lead.product_id,
                    lead_id=lead.id,
                    calendar_event_id=event_id,
                    start_time=start,
                    end_time=start + timedelta(minutes=30),
                    timezone="Asia/Kolkata",
                    status=BookingStatus.CONFIRMED,
                )
            )
        with pytest.raises(IntegrityError):
            await session.flush()

    async def test_a_cancelled_booking_frees_the_slot(self, session: AsyncSession) -> None:
        """The index is partial, so a cancellation must not block rebooking."""
        first = await _make_lead(session)
        second = await _make_lead(session, "+919000000002")
        start = datetime(2026, 9, 1, 11, tzinfo=UTC)

        session.add(
            Booking(
                product_id=first.product_id,
                lead_id=first.id,
                calendar_event_id="evt-cancelled",
                start_time=start,
                end_time=start + timedelta(minutes=30),
                timezone="Asia/Kolkata",
                status=BookingStatus.CANCELLED,
            )
        )
        session.add(
            Booking(
                product_id=second.product_id,
                lead_id=second.id,
                calendar_event_id="evt-new",
                start_time=start,
                end_time=start + timedelta(minutes=30),
                timezone="Asia/Kolkata",
                status=BookingStatus.CONFIRMED,
            )
        )
        await session.flush()  # must not raise

    async def test_timezone_survives_the_round_trip(self, session: AsyncSession) -> None:
        lead = await _make_lead(session)
        start = datetime(2026, 9, 1, 10, tzinfo=UTC)
        booking = Booking(
            product_id=lead.product_id,
            lead_id=lead.id,
            start_time=start,
            end_time=start + timedelta(minutes=30),
            timezone="Asia/Kolkata",
        )
        session.add(booking)
        await session.flush()
        await session.refresh(booking)

        assert booking.timezone == "Asia/Kolkata"
        assert booking.start_time.tzinfo is not None


class TestNotificationFanout:
    async def test_repeated_fanout_for_one_booking_is_rejected(self, session: AsyncSession) -> None:
        """[D3] fans out per active user; §20 requires the retry to be idempotent."""
        user = await _make_user(session)
        booking_id = uuid.uuid4()

        for _ in range(2):
            session.add(
                Notification(
                    user_id=user.id,
                    type=NotificationType.DEMO_BOOKED,
                    title="New Demo Booked",
                    message="Rahul has booked a demo.",
                    related_entity_type="booking",
                    related_entity_id=booking_id,
                )
            )
        with pytest.raises(IntegrityError):
            await session.flush()

    async def test_each_user_gets_their_own_row(self, session: AsyncSession) -> None:
        first = await _make_user(session, "a@example.com")
        second = await _make_user(session, "b@example.com")
        booking_id = uuid.uuid4()

        for user in (first, second):
            session.add(
                Notification(
                    user_id=user.id,
                    type=NotificationType.DEMO_BOOKED,
                    title="New Demo Booked",
                    message="Rahul has booked a demo.",
                    related_entity_type="booking",
                    related_entity_id=booking_id,
                )
            )
        await session.flush()  # must not raise


class TestAudienceStoresRejectedRows:
    async def test_duplicate_and_invalid_rows_are_storable(self, session: AsyncSession) -> None:
        """§6: the preview must show what was rejected and why."""
        audience = Audience(name="Q3 list", product_id=await ensure_product_id(session))
        session.add(audience)
        await session.flush()

        for row_number, status in (
            (1, ContactValidationStatus.VALID),
            (2, ContactValidationStatus.DUPLICATE),
            (3, ContactValidationStatus.INVALID),
        ):
            session.add(
                AudienceContact(
                    audience_id=audience.id,
                    row_number=row_number,
                    phone_e164=PHONE,
                    validation_status=status,
                    validation_error=None if status is ContactValidationStatus.VALID else "reason",
                    raw_row={"Phone": PHONE},
                )
            )
        await session.flush()  # same phone three times must not raise

        count = (
            await session.execute(
                text("SELECT count(*) FROM audience_contacts WHERE audience_id = :aid"),
                {"aid": audience.id},
            )
        ).scalar_one()
        assert count == 3


class TestVectorStorageAndSearch:
    async def test_vector_round_trips(self, session: AsyncSession) -> None:
        chunk = await self._make_chunk(session, [0.1] * 384, index=0)
        await session.refresh(chunk)

        assert chunk.embedding is not None
        assert len(chunk.embedding) == 384

    async def test_wrong_dimension_is_rejected(self, session: AsyncSession) -> None:
        """[D1]: incompatible vectors must never enter the column."""
        with pytest.raises(Exception):  # noqa: B017 - driver-specific DataError
            await self._make_chunk(session, [0.1] * 128, index=1)

    async def test_cosine_similarity_orders_results(self, session: AsyncSession) -> None:
        """Proves the pgvector operator works, which Phase 8 retrieval depends on."""
        near = [1.0] + [0.0] * 383
        far = [0.0] * 383 + [1.0]

        await self._make_chunk(session, near, index=0, content="near")
        await self._make_chunk(session, far, index=1, content="far")

        query = [1.0] + [0.0] * 383
        rows = (
            (
                await session.execute(
                    select(KnowledgeChunk.content).order_by(
                        KnowledgeChunk.embedding.cosine_distance(query)
                    )
                )
            )
            .scalars()
            .all()
        )

        assert rows[0] == "near"
        assert rows[-1] == "far"

    async def test_chunk_index_is_unique_per_document(self, session: AsyncSession) -> None:
        chunk = await self._make_chunk(session, [0.1] * 384, index=0)
        session.add(
            KnowledgeChunk(document_id=chunk.document_id, chunk_index=0, content="duplicate")
        )
        with pytest.raises(IntegrityError):
            await session.flush()

    async def _make_chunk(
        self,
        session: AsyncSession,
        embedding: list[float],
        *,
        index: int,
        content: str = "chunk",
    ) -> KnowledgeChunk:
        existing = (await session.execute(select(KnowledgeDocument).limit(1))).scalar_one_or_none()
        if existing is None:
            knowledge_base = KnowledgeBase(name="Product docs")
            session.add(knowledge_base)
            await session.flush()
            existing = KnowledgeDocument(knowledge_base_id=knowledge_base.id, title="Pricing")
            session.add(existing)
            await session.flush()

        chunk = KnowledgeChunk(
            document_id=existing.id,
            chunk_index=index,
            content=content,
            embedding=embedding,
            embedding_model="fake/deterministic-embedding",
        )
        session.add(chunk)
        await session.flush()
        return chunk


class TestCascades:
    async def test_deleting_an_audience_removes_its_contacts(self, session: AsyncSession) -> None:
        audience = Audience(name="Temp", product_id=await ensure_product_id(session))
        session.add(audience)
        await session.flush()
        session.add(
            AudienceContact(
                audience_id=audience.id,
                row_number=1,
                validation_status=ContactValidationStatus.VALID,
                raw_row={},
            )
        )
        await session.flush()

        await session.delete(audience)
        await session.flush()

        remaining = (await session.execute(select(AudienceContact))).scalars().all()
        assert remaining == []
