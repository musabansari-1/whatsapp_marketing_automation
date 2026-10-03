"""Booking and notification flow tests (§12, §13, §20, [D3], [D21]).

The critical sequence, end to end against real PostgreSQL:

    customer selects → recheck → Calendar event → booking → notification → confirmation

The failures matter more than the happy path, because each one is a way to tell a
customer something untrue or to leave the sales team unaware of a demo.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import tools as ai_tools
from app.domain.enums import (
    BookingStatus,
    ConversationStatus,
    LeadStatus,
    MessageDirection,
    NotificationType,
    UserRole,
)
from app.domain.qualification import QualificationRules
from app.domain.slots import SlotPolicy
from app.integrations.calendar import FakeCalendarProvider
from app.integrations.queue import TaskRequest, TaskType
from app.integrations.whatsapp import FakeWhatsAppProvider
from app.persistence.models import (
    Booking,
    Conversation,
    ConversationMessage,
    Lead,
    Message,
    Notification,
    User,
)
from app.persistence.repositories import conversations as conversation_repo
from app.worker.handlers.booking import notify_booking
from tests.integration.helpers import (
    ensure_product_id,
    get_test_database_url,
    run_db,
)

pytestmark = pytest.mark.integration

IST = ZoneInfo("Asia/Kolkata")
PHONE = "+919876500200"
NOW = datetime(2026, 9, 2, 9, 0, tzinfo=IST)


def a_policy() -> SlotPolicy:
    return SlotPolicy(
        duration_minutes=30,
        buffer_minutes=15,
        working_days=frozenset({0, 1, 2, 3, 4}),
        working_start=datetime(2026, 1, 1, 10, 0).time(),
        working_end=datetime(2026, 1, 1, 18, 0).time(),
        horizon_days=14,
        timezone="Asia/Kolkata",
    )


@pytest.fixture
def booking_env(engine: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeWhatsAppProvider]:
    from app.core.config import reset_settings_cache
    from app.persistence import database

    monkeypatch.setenv("DATABASE_URL", get_test_database_url())
    reset_settings_cache()
    database._engine = None  # noqa: SLF001
    database._session_factory = None  # noqa: SLF001

    # The notify task sends no WhatsApp message — the turn's reply is the
    # confirmation — so there is no provider to fake here. The fake stays as
    # the assertion surface proving nothing was sent.
    whatsapp = FakeWhatsAppProvider()

    async def clear(session: AsyncSession) -> None:
        await session.execute(delete(Notification))
        await session.execute(delete(Booking))
        await session.execute(delete(ConversationMessage))
        await session.execute(delete(Message))
        await session.execute(delete(Conversation))
        await session.execute(delete(Lead))
        await session.execute(delete(User))

    run_db(clear)
    yield whatsapp
    run_db(clear)


def seed(*, users: int = 2, lead_status: LeadStatus = LeadStatus.QUALIFIED) -> tuple[str, str]:
    """A lead with an open conversation, and some internal users to notify."""

    async def create(session: AsyncSession) -> tuple[str, str]:
        from app.core.security import hash_password

        for index in range(users):
            session.add(
                User(
                    email=f"user{index}@example.com",
                    password_hash=hash_password("a-sufficiently-long-password"),
                    role=UserRole.ADMIN if index == 0 else UserRole.SALESPERSON,
                )
            )

        product_id = await ensure_product_id(session)
        lead = Lead(
            phone_e164=PHONE,
            full_name="Asha Rao",
            company="Northwind",
            status=lead_status,
            product_id=product_id,
        )
        session.add(lead)
        await session.flush()

        conversation = Conversation(
            lead_id=lead.id, status=ConversationStatus.ACTIVE, product_id=product_id
        )
        session.add(conversation)
        await session.flush()
        return str(lead.id), str(conversation.id)

    return run_db(create)


def book(
    lead_id: str,
    conversation_id: str,
    *,
    calendar: FakeCalendarProvider,
) -> ai_tools.ToolResult:
    """List slots and book the first, as the agent does."""

    async def call(session: AsyncSession) -> ai_tools.ToolResult:
        lead = await conversation_repo.get_lead(session, uuid.UUID(lead_id))
        conversation = await conversation_repo.get_conversation(session, uuid.UUID(conversation_id))
        assert lead is not None and conversation is not None

        context = ai_tools.ToolContext(
            session=session,
            lead=lead,
            conversation=conversation,
            slot_policy=a_policy(),
            calendar=calendar,
            qualification_rules=QualificationRules(),
            now=NOW,
        )

        slots = await ai_tools.dispatch("get_available_slots", {}, context)
        return await ai_tools.dispatch(
            "book_demo", {"slot_id": slots.data["slots"][0]["slot_id"]}, context
        )

    return run_db(call)


def run_notify(booking_id: str) -> dict[str, Any]:
    from app.persistence import database

    async def run() -> dict[str, Any]:
        database._engine = None  # noqa: SLF001
        database._session_factory = None  # noqa: SLF001
        try:
            result = await notify_booking(
                TaskRequest(
                    task_type=TaskType.NOTIFY_BOOKING,
                    task_id=f"notify-booking-{booking_id}",
                    payload={"booking_id": booking_id},
                )
            )
            return dict(result)
        finally:
            await database.dispose_engine()

    return asyncio.run(run())


def bookings() -> list[Booking]:
    async def fetch(session: AsyncSession) -> list[Booking]:
        result = await session.execute(select(Booking))
        return list(result.scalars().all())

    return run_db(fetch)


def notifications() -> list[Notification]:
    async def fetch(session: AsyncSession) -> list[Notification]:
        result = await session.execute(select(Notification))
        return list(result.scalars().all())

    return run_db(fetch)


class TestBookingRecord:
    def test_a_confirmed_booking_is_recorded(self, booking_env: FakeWhatsAppProvider) -> None:
        """§12: the booking row exists only once Calendar has confirmed."""
        lead_id, conversation_id = seed()
        calendar = FakeCalendarProvider()

        result = book(lead_id, conversation_id, calendar=calendar)

        assert result.ok
        rows = bookings()
        assert len(rows) == 1
        assert rows[0].status is BookingStatus.CONFIRMED
        assert rows[0].calendar_event_id == result.data["event_id"]
        assert rows[0].confirmed_at is not None
        assert rows[0].meeting_link
        assert result.data["meeting_link"] == rows[0].meeting_link
        # The platform has no email path, stated as data so the model does not
        # infer a sent invite from the lead's email (staging 2026-09-28).
        assert result.data["email_sent"] is False
        assert calendar.created_requests[0].create_meeting is True

    def test_the_booking_carries_its_own_timezone(self, booking_env: FakeWhatsAppProvider) -> None:
        """[D8]: a later policy change must not retroactively reinterpret it."""
        lead_id, conversation_id = seed()

        book(lead_id, conversation_id, calendar=FakeCalendarProvider())

        assert bookings()[0].timezone == "Asia/Kolkata"

    def test_a_static_meeting_link_is_shared_with_the_customer(
        self, booking_env: FakeWhatsAppProvider
    ) -> None:
        """Option 2 ([D65]): the product's permanent link reaches the booking row."""
        lead_id, conversation_id = seed()
        calendar = FakeCalendarProvider(
            static_meeting_link="https://meet.google.com/static-room-abc"
        )

        result = book(lead_id, conversation_id, calendar=calendar)

        assert result.ok
        assert result.data["meeting_link"] == "https://meet.google.com/static-room-abc"
        assert bookings()[0].meeting_link == "https://meet.google.com/static-room-abc"

    def test_booking_twice_on_one_listing_records_one_row(
        self, booking_env: FakeWhatsAppProvider
    ) -> None:
        """§20: booking the same slot ID twice — a double tool call in one turn —
        converges on one event, one row."""
        lead_id, conversation_id = seed()
        calendar = FakeCalendarProvider()

        async def call(session: AsyncSession) -> None:
            lead = await conversation_repo.get_lead(session, uuid.UUID(lead_id))
            conversation = await conversation_repo.get_conversation(
                session, uuid.UUID(conversation_id)
            )
            assert lead is not None and conversation is not None
            context = ai_tools.ToolContext(
                session=session,
                lead=lead,
                conversation=conversation,
                slot_policy=a_policy(),
                calendar=calendar,
                qualification_rules=QualificationRules(),
                now=NOW,
            )
            slots = await ai_tools.dispatch("get_available_slots", {}, context)
            first = await ai_tools.dispatch(
                "book_demo", {"slot_id": slots.data["slots"][0]["slot_id"]}, context
            )
            second = await ai_tools.dispatch(
                "book_demo", {"slot_id": slots.data["slots"][0]["slot_id"]}, context
            )
            assert first.ok and second.ok

        run_db(call)

        assert len(bookings()) == 1
        assert len(calendar.events) == 1

    def test_a_fresh_listing_after_a_booking_states_the_booking(
        self, booking_env: FakeWhatsAppProvider
    ) -> None:
        """Per-listing slot IDs mean a later turn cannot re-attempt the booked time
        by copying it — so the listing states the existing booking, and the model
        confirms it instead of booking a second demo beside it."""
        lead_id, conversation_id = seed()
        calendar = FakeCalendarProvider()
        assert book(lead_id, conversation_id, calendar=calendar).ok

        async def relist(session: AsyncSession) -> Any:
            lead = await conversation_repo.get_lead(session, uuid.UUID(lead_id))
            conversation = await conversation_repo.get_conversation(
                session, uuid.UUID(conversation_id)
            )
            assert lead is not None and conversation is not None
            context = ai_tools.ToolContext(
                session=session,
                lead=lead,
                conversation=conversation,
                slot_policy=a_policy(),
                calendar=calendar,
                qualification_rules=QualificationRules(),
                now=NOW,
            )
            return await ai_tools.dispatch("get_available_slots", {}, context)

        result = run_db(relist)

        assert result.ok
        [existing] = result.data["existing_bookings"]
        assert existing["local_time"] == "Wednesday 02 September, 10:00 AM"
        assert "meet.google.com" in existing["meeting_link"]

    def test_nothing_is_recorded_when_the_calendar_write_fails(
        self, booking_env: FakeWhatsAppProvider
    ) -> None:
        """CLAUDE.md: never claim a demo is booked before Calendar confirms it. No
        event means no booking row and no notification."""

        class WriteFailingCalendar(FakeCalendarProvider):
            async def create_event(self, request: Any) -> Any:
                raise RuntimeError("calendar write failed")

        lead_id, conversation_id = seed()

        result = book(lead_id, conversation_id, calendar=WriteFailingCalendar())

        assert result.ok is False
        assert bookings() == []

    def test_the_lead_advances_to_demo_booked(self, booking_env: FakeWhatsAppProvider) -> None:
        """Proposed by the tool, applied by the caller through the §10 table."""
        lead_id, conversation_id = seed(lead_status=LeadStatus.QUALIFIED)
        calendar = FakeCalendarProvider()

        async def call(session: AsyncSession) -> Any:
            lead = await conversation_repo.get_lead(session, uuid.UUID(lead_id))
            conversation = await conversation_repo.get_conversation(
                session, uuid.UUID(conversation_id)
            )
            assert lead is not None and conversation is not None

            context = ai_tools.ToolContext(
                session=session,
                lead=lead,
                conversation=conversation,
                slot_policy=a_policy(),
                calendar=calendar,
                qualification_rules=QualificationRules(),
                now=NOW,
            )
            slots = await ai_tools.dispatch("get_available_slots", {}, context)
            await ai_tools.dispatch(
                "book_demo", {"slot_id": slots.data["slots"][0]["slot_id"]}, context
            )
            return context.proposed_status

        assert run_db(call) is LeadStatus.DEMO_BOOKED


class TestNotificationFanOut:
    def test_every_active_user_is_notified(self, booking_env: FakeWhatsAppProvider) -> None:
        """[D3]: V1 has no lead assignment, so the whole team is told."""
        lead_id, conversation_id = seed(users=3)
        book(lead_id, conversation_id, calendar=FakeCalendarProvider())

        result = run_notify(str(bookings()[0].id))

        assert result["notified"] == 3
        assert len(notifications()) == 3

    def test_an_inactive_user_is_not_notified(self, booking_env: FakeWhatsAppProvider) -> None:
        lead_id, conversation_id = seed(users=2)

        async def deactivate(session: AsyncSession) -> None:
            result = await session.execute(select(User).limit(1))
            user = result.scalars().one()
            user.is_active = False

        run_db(deactivate)
        book(lead_id, conversation_id, calendar=FakeCalendarProvider())

        assert run_notify(str(bookings()[0].id))["notified"] == 1

    def test_re_running_notifies_nobody_twice(self, booking_env: FakeWhatsAppProvider) -> None:
        """§12: a confirmed booking stays confirmed while a failed notification
        retries — so the retry must re-run for everyone and duplicate for nobody."""
        lead_id, conversation_id = seed(users=3)
        book(lead_id, conversation_id, calendar=FakeCalendarProvider())
        booking_id = str(bookings()[0].id)

        first = run_notify(booking_id)
        second = run_notify(booking_id)

        assert first["notified"] == 3
        assert second["notified"] == 0
        assert len(notifications()) == 3

    def test_the_notification_names_the_customer_and_the_time(
        self, booking_env: FakeWhatsAppProvider
    ) -> None:
        lead_id, conversation_id = seed(users=1)
        book(lead_id, conversation_id, calendar=FakeCalendarProvider())
        booking = bookings()[0]
        run_notify(str(booking.id))

        message = notifications()[0].message
        assert "Asha Rao" in message
        assert "Northwind" in message
        assert booking.meeting_link
        assert booking.meeting_link in message

    def test_the_notification_links_to_the_booking(self, booking_env: FakeWhatsAppProvider) -> None:
        """§13 relates a notification to what it is about, so the dashboard can link."""
        lead_id, conversation_id = seed(users=1)
        book(lead_id, conversation_id, calendar=FakeCalendarProvider())
        booking_id = bookings()[0].id

        run_notify(str(booking_id))

        notification = notifications()[0]
        assert notification.type is NotificationType.DEMO_BOOKED
        assert notification.related_entity_id == booking_id

    def test_a_booking_with_no_active_users_still_confirms(
        self, booking_env: FakeWhatsAppProvider
    ) -> None:
        """Nobody to tell is not a reason to fail a booking the customer made."""
        lead_id, conversation_id = seed(users=0)
        book(lead_id, conversation_id, calendar=FakeCalendarProvider())

        result = run_notify(str(bookings()[0].id))

        assert result["notified"] == 0
        assert bookings()[0].status is BookingStatus.CONFIRMED


def run_notify_failed(lead_id: str, reason: str = "the calendar was unreachable") -> dict[str, Any]:
    """Run the failed-booking notification task, as the worker would."""
    from app.persistence import database
    from app.worker.handlers.booking_failed import notify_booking_failed

    async def run() -> dict[str, Any]:
        database._engine = None  # noqa: SLF001
        database._session_factory = None  # noqa: SLF001
        try:
            result = await notify_booking_failed(
                TaskRequest(
                    task_type=TaskType.NOTIFY_BOOKING_FAILED,
                    task_id=f"notify-booking-failed-{lead_id}",
                    payload={"lead_id": lead_id, "reason": reason},
                )
            )
            return dict(result)
        finally:
            await database.dispose_engine()

    return asyncio.run(run())


class TestBookingFailedNotification:
    """The bell behind the fallback promise: a terminal booking failure tells the
    customer a colleague will follow up, so the team has to hear about it —
    staging lost an Academicae demo to a silent failure on 2026-10-01."""

    def test_every_active_user_is_notified_once(self, booking_env: FakeWhatsAppProvider) -> None:
        lead_id, _ = seed(users=3)

        result = run_notify_failed(lead_id)

        assert result["notified"] == 3
        assert len(notifications()) == 3

    def test_a_repeat_failure_does_not_re_announce(self, booking_env: FakeWhatsAppProvider) -> None:
        """The fan-out's unique constraint collapses repeats: a bell the team
        learns to ignore is worse than none."""
        lead_id, _ = seed(users=2)

        first = run_notify_failed(lead_id)
        second = run_notify_failed(lead_id)

        assert first["notified"] == 2
        assert second["notified"] == 0
        assert len(notifications()) == 2

    def test_the_notification_names_the_customer_and_the_reason(
        self, booking_env: FakeWhatsAppProvider
    ) -> None:
        lead_id, _ = seed(users=1)

        run_notify_failed(lead_id, reason="the calendar was unreachable")

        notification = notifications()[0]
        assert notification.type is NotificationType.BOOKING_FAILED
        assert "Asha Rao" in notification.message
        assert "+919876500200" in notification.message
        assert "the calendar was unreachable" in notification.message
        assert "will follow up" in notification.message


class TestCustomerConfirmation:
    """The turn's reply is the confirmation — the notify task sends no second
    WhatsApp message. Staging double-texted every booking until the separate
    confirmation was removed."""

    def test_the_notify_task_sends_no_whatsapp_message(
        self, booking_env: FakeWhatsAppProvider
    ) -> None:
        lead_id, conversation_id = seed(users=1)
        book(lead_id, conversation_id, calendar=FakeCalendarProvider())

        result = run_notify(str(bookings()[0].id))

        assert result["notified"] == 1
        assert "confirmed" not in result
        assert booking_env.sent_texts == []

    def test_a_retried_task_still_notifies_each_user_once(
        self, booking_env: FakeWhatsAppProvider
    ) -> None:
        """§20: the fan-out's unique constraint collapses the retry."""
        lead_id, conversation_id = seed(users=1)
        book(lead_id, conversation_id, calendar=FakeCalendarProvider())
        booking_id = str(bookings()[0].id)

        run_notify(booking_id)
        run_notify(booking_id)

        assert len(notifications()) == 1
        assert booking_env.sent_texts == []

    def test_no_outbound_row_is_written_by_the_notify_task(
        self, booking_env: FakeWhatsAppProvider
    ) -> None:
        lead_id, conversation_id = seed(users=1)
        book(lead_id, conversation_id, calendar=FakeCalendarProvider())

        run_notify(str(bookings()[0].id))

        async def outbound(session: AsyncSession) -> list[str | None]:
            result = await session.execute(
                select(Message.idempotency_key).where(
                    Message.direction == MessageDirection.OUTBOUND
                )
            )
            return list(result.scalars().all())

        assert run_db(outbound) == []


class TestRescheduleCancelsSuperseded:
    """A "change my demo" books new without touching the old rows. The notify task
    cancels this conversation's older confirmed bookings after the customer
    already has the new confirmation — never before it, so the turn's latency is
    untouched."""

    def _book_two(self) -> tuple[Any, Any, FakeCalendarProvider]:
        lead_id, conversation_id = seed(users=1)
        calendar = FakeCalendarProvider()
        assert book(lead_id, conversation_id, calendar=calendar).ok
        assert book(lead_id, conversation_id, calendar=calendar).ok
        rows = sorted(bookings(), key=lambda row: row.start_time)
        assert len(rows) == 2
        return rows[0], rows[1], calendar

    def _use_calendar(
        self, monkeypatch: pytest.MonkeyPatch, calendar: FakeCalendarProvider
    ) -> None:
        """The notify task resolves its own stateless calendar client; point it at
        the test's fake so event deletion is observable."""
        monkeypatch.setattr(
            "app.worker.handlers.booking.resolve_booking_capability",
            lambda settings, product: (None, calendar),
        )

    def test_older_bookings_are_cancelled_and_their_events_deleted(
        self, booking_env: FakeWhatsAppProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        older, newer, calendar = self._book_two()
        self._use_calendar(monkeypatch, calendar)

        result = run_notify(str(newer.id))

        assert result["cancelled"] == 1
        assert _status(older.id) is BookingStatus.CANCELLED
        assert _status(newer.id) is BookingStatus.CONFIRMED
        assert older.calendar_event_id not in calendar.events
        assert newer.calendar_event_id in calendar.events

    def test_notify_for_the_only_booking_cancels_nothing(
        self, booking_env: FakeWhatsAppProvider
    ) -> None:
        lead_id, conversation_id = seed(users=1)
        book(lead_id, conversation_id, calendar=FakeCalendarProvider())

        result = run_notify(str(bookings()[0].id))

        assert result["cancelled"] == 0
        assert bookings()[0].status is BookingStatus.CONFIRMED

    def test_a_rerun_cancels_nothing_twice(
        self, booking_env: FakeWhatsAppProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The first run already cancelled the older row; the retry finds no
        CONFIRMED older booking and changes nothing."""
        older, newer, calendar = self._book_two()
        self._use_calendar(monkeypatch, calendar)

        run_notify(str(newer.id))
        result = run_notify(str(newer.id))

        assert result["cancelled"] == 0
        assert _status(older.id) is BookingStatus.CANCELLED
        assert _status(newer.id) is BookingStatus.CONFIRMED

    def test_a_failed_event_delete_keeps_the_row_confirmed(
        self, booking_env: FakeWhatsAppProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Diary truth over dashboard tidiness: an event that survives keeps its
        row, loudly logged, rather than a dashboard that says free while Google
        says busy."""

        class UndeletableCalendar(FakeCalendarProvider):
            async def delete_event(self, event_id: str) -> None:
                raise RuntimeError("calendar delete failed")

        lead_id, conversation_id = seed(users=1)
        calendar = UndeletableCalendar()
        assert book(lead_id, conversation_id, calendar=calendar).ok
        assert book(lead_id, conversation_id, calendar=calendar).ok
        rows = sorted(bookings(), key=lambda row: row.start_time)
        self._use_calendar(monkeypatch, calendar)

        result = run_notify(str(rows[1].id))

        assert result["cancelled"] == 0
        assert _status(rows[0].id) is BookingStatus.CONFIRMED
        assert _status(rows[1].id) is BookingStatus.CONFIRMED


def _status(booking_id: Any) -> BookingStatus:
    async def fetch(session: AsyncSession) -> BookingStatus:
        row = await session.get(Booking, uuid.UUID(str(booking_id)))
        assert row is not None
        return row.status

    return run_db(fetch)


class TestTaskValidation:
    def test_a_missing_booking_id_is_rejected(self, booking_env: FakeWhatsAppProvider) -> None:
        with pytest.raises(ValueError, match="booking_id"):
            asyncio.run(
                notify_booking(
                    TaskRequest(task_type=TaskType.NOTIFY_BOOKING, task_id="x", payload={})
                )
            )

    def test_an_unknown_booking_is_rejected(self, booking_env: FakeWhatsAppProvider) -> None:
        with pytest.raises(ValueError, match="not found"):
            run_notify(str(uuid.uuid4()))
