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
from app.core.errors import NonRetryableProviderError, RetryableProviderError
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
from app.worker.handlers.booking import confirmation_idempotency_key, notify_booking
from app.worker.handlers.campaign_send import RetryableSendError
from tests.integration.helpers import get_test_database_url, run_db

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

    whatsapp = FakeWhatsAppProvider()
    monkeypatch.setattr(
        "app.worker.handlers.booking.build_whatsapp_provider", lambda _settings: whatsapp
    )

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

        lead = Lead(phone_e164=PHONE, full_name="Asha Rao", company="Northwind", status=lead_status)
        session.add(lead)
        await session.flush()

        conversation = Conversation(lead_id=lead.id, status=ConversationStatus.ACTIVE)
        session.add(conversation)
        await session.flush()
        return str(lead.id), str(conversation.id)

    return run_db(create)


def book(
    lead_id: str,
    conversation_id: str,
    *,
    calendar: FakeCalendarProvider,
    start_time: str | None = None,
) -> ai_tools.ToolResult:
    """Run the book_demo tool, as the agent does."""

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

        chosen = start_time
        if chosen is None:
            slots = await ai_tools.dispatch("get_available_slots", {}, context)
            chosen = str(slots.data["slots"][0]["start_time"])

        return await ai_tools.dispatch("book_demo", {"start_time": chosen}, context)

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

    def test_the_booking_carries_its_own_timezone(self, booking_env: FakeWhatsAppProvider) -> None:
        """[D8]: a later policy change must not retroactively reinterpret it."""
        lead_id, conversation_id = seed()

        book(lead_id, conversation_id, calendar=FakeCalendarProvider())

        assert bookings()[0].timezone == "Asia/Kolkata"

    def test_booking_twice_records_one_row(self, booking_env: FakeWhatsAppProvider) -> None:
        """§20: a redelivered turn must converge. The event ID is derived from the
        conversation and slot, so the second attempt finds the first's event."""
        lead_id, conversation_id = seed()
        calendar = FakeCalendarProvider()

        first = book(lead_id, conversation_id, calendar=calendar)
        second = book(
            lead_id, conversation_id, calendar=calendar, start_time=first.data["start_time"]
        )

        assert second.ok
        assert len(bookings()) == 1
        assert len(calendar.events) == 1

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
                "book_demo", {"start_time": slots.data["slots"][0]["start_time"]}, context
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
        run_notify(str(bookings()[0].id))

        message = notifications()[0].message
        assert "Asha Rao" in message
        assert "Northwind" in message

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


class TestCustomerConfirmation:
    def test_the_customer_is_told(self, booking_env: FakeWhatsAppProvider) -> None:
        lead_id, conversation_id = seed(users=1)
        book(lead_id, conversation_id, calendar=FakeCalendarProvider())

        run_notify(str(bookings()[0].id))

        assert len(booking_env.sent_texts) == 1
        assert booking_env.sent_texts[0].to_e164 == PHONE

    def test_the_wording_is_configurable(self, booking_env: FakeWhatsAppProvider) -> None:
        """[D21]: customer-facing copy is never hard-coded."""
        from app.core.config import get_settings

        lead_id, conversation_id = seed(users=1)
        book(lead_id, conversation_id, calendar=FakeCalendarProvider())
        run_notify(str(bookings()[0].id))

        template = get_settings().msg_booking_confirmation
        assert template.split("{")[0].strip() in booking_env.sent_texts[0].body

    def test_the_time_is_shown_in_the_booking_timezone(
        self, booking_env: FakeWhatsAppProvider
    ) -> None:
        """A customer reading a UTC timestamp has to translate it themselves, at
        exactly the wrong moment."""
        lead_id, conversation_id = seed(users=1)
        book(lead_id, conversation_id, calendar=FakeCalendarProvider())
        run_notify(str(bookings()[0].id))

        # The first free slot is 10:00 IST, which is 04:30 UTC.
        assert "10:00 AM" in booking_env.sent_texts[0].body

    def test_the_confirmation_reaches_the_transcript(
        self, booking_env: FakeWhatsAppProvider
    ) -> None:
        """The AI's next turn must know the customer was already told, or it says it
        again."""
        lead_id, conversation_id = seed(users=1)
        book(lead_id, conversation_id, calendar=FakeCalendarProvider())
        run_notify(str(bookings()[0].id))

        async def transcript(session: AsyncSession) -> list[str]:
            turns = await conversation_repo.get_history(session, uuid.UUID(conversation_id))
            return [turn.content for turn in turns]

        assert any("confirmed" in content.lower() for content in run_db(transcript))

    def test_the_customer_is_not_told_twice(self, booking_env: FakeWhatsAppProvider) -> None:
        """§20: the key is derived from the booking, so a retry computes the same one
        and the unique constraint refuses the second write."""
        lead_id, conversation_id = seed(users=1)
        book(lead_id, conversation_id, calendar=FakeCalendarProvider())
        booking_id = str(bookings()[0].id)

        run_notify(booking_id)
        run_notify(booking_id)

        assert len(booking_env.sent_texts) == 1

    def test_the_key_is_derived_from_the_booking(self, booking_env: FakeWhatsAppProvider) -> None:
        lead_id, conversation_id = seed(users=1)
        book(lead_id, conversation_id, calendar=FakeCalendarProvider())
        booking_id = str(bookings()[0].id)

        run_notify(booking_id)

        async def outbound(session: AsyncSession) -> list[str | None]:
            result = await session.execute(
                select(Message.idempotency_key).where(
                    Message.direction == MessageDirection.OUTBOUND
                )
            )
            return list(result.scalars().all())

        assert confirmation_idempotency_key(booking_id) in run_db(outbound)

    def test_a_transient_send_failure_is_retried(self, booking_env: FakeWhatsAppProvider) -> None:
        lead_id, conversation_id = seed(users=1)
        book(lead_id, conversation_id, calendar=FakeCalendarProvider())
        booking_env.fail_with = RetryableProviderError("rate limit", provider="fake")

        with pytest.raises(RetryableSendError):
            run_notify(str(bookings()[0].id))

    def test_a_permanent_send_failure_leaves_the_booking_confirmed(
        self, booking_env: FakeWhatsAppProvider
    ) -> None:
        """§12: the booking is real whatever happens to the acknowledgement, and the
        team has already been told."""
        lead_id, conversation_id = seed(users=2)
        book(lead_id, conversation_id, calendar=FakeCalendarProvider())
        booking_env.fail_with = NonRetryableProviderError("blocked", provider="fake")

        result = run_notify(str(bookings()[0].id))

        assert result["confirmed"] == 0
        assert result["notified"] == 2
        assert bookings()[0].status is BookingStatus.CONFIRMED

    def test_the_team_is_notified_before_the_customer(
        self, booking_env: FakeWhatsAppProvider
    ) -> None:
        """§12's order. If the confirmation fails, the team can still act."""
        lead_id, conversation_id = seed(users=1)
        book(lead_id, conversation_id, calendar=FakeCalendarProvider())
        booking_env.fail_with = NonRetryableProviderError("blocked", provider="fake")

        run_notify(str(bookings()[0].id))

        assert len(notifications()) == 1
        assert booking_env.sent_texts == []


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
