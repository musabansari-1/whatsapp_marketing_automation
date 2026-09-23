"""Per-product calendars and booking ownership (Phase 24, [D43], [D52], §28.8).

Each product books into its own Google Calendar through one shared operations account.
So the same wall-clock hour can be confirmed in every product at once, while within one
product it can still be confirmed only once — and nothing a conversation says can move a
booking onto another product's calendar.

`FakeCalendarProvider` instances stand in for distinct Google calendars. Provider
selection is also read off the real `GoogleCalendarProvider`, because that is where a
per-product value could bleed between products.

Products are created dynamically. Nothing depends on Academicae's identity.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import tools as ai_tools
from app.core.config import Settings
from app.domain.enums import BookingStatus, ConversationStatus, LeadStatus, UserRole
from app.domain.qualification import QualificationRules
from app.domain.slots import SlotPolicy, policy_from_settings
from app.integrations.calendar import FakeCalendarProvider, TimeInterval
from app.integrations.calendar.google import GoogleCalendarProvider
from app.integrations.queue import TaskRequest, TaskType
from app.integrations.registry import CalendarNotConfiguredError, build_calendar_provider
from app.integrations.whatsapp import FakeWhatsAppProvider
from app.persistence.models import (
    Booking,
    Conversation,
    ConversationMessage,
    Lead,
    Message,
    Notification,
    Product,
    User,
)
from app.persistence.repositories import conversations as conversation_repo
from app.worker.handlers.booking import notify_booking
from app.worker.handlers.conversation import resolve_booking_capability
from tests.integration.helpers import get_test_database_url, run_db

pytestmark = pytest.mark.integration

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 9, 2, 9, 0, tzinfo=IST)  # a Wednesday morning, before working hours
TUESDAY_10AM = datetime(2026, 9, 8, 10, 0, tzinfo=IST)
HALF_HOUR = timedelta(minutes=30)
PHONE = "+919876500710"


@pytest.fixture(autouse=True)
def calendar_env(engine: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeWhatsAppProvider]:
    """A clean slate, and a database the booking follow-up task can reach on its own."""
    from app.core.config import reset_settings_cache
    from app.persistence import database

    monkeypatch.setenv("DATABASE_URL", get_test_database_url())
    reset_settings_cache()
    database._engine = None  # noqa: SLF001
    database._session_factory = None  # noqa: SLF001

    whatsapp = FakeWhatsAppProvider()
    monkeypatch.setattr(
        "app.worker.handlers.booking.build_whatsapp_provider", lambda _settings, **_: whatsapp
    )

    async def clear(session: AsyncSession) -> None:
        await session.execute(delete(Notification))
        await session.execute(delete(Booking))
        await session.execute(delete(ConversationMessage))
        await session.execute(delete(Message))
        await session.execute(delete(Conversation))
        await session.execute(delete(Lead))
        await session.execute(delete(User))
        await session.execute(delete(Product).where(Product.slug.like("k-%")))

    run_db(clear)
    yield whatsapp
    run_db(clear)


# ---------------------------------------------------------------- helpers


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


def make_product(
    slug: str, calendar_id: str | None, *, active: bool = True, **overrides: Any
) -> uuid.UUID:
    async def create(session: AsyncSession) -> uuid.UUID:
        product = Product(
            slug=slug,
            display_name=slug.upper(),
            is_active=active,
            google_calendar_id=calendar_id,
            **overrides,
        )
        session.add(product)
        await session.flush()
        return product.id

    return run_db(create)


def load_product(product_id: uuid.UUID) -> Product:
    async def read(session: AsyncSession) -> Product:
        product = await session.get(Product, product_id)
        assert product is not None
        return product

    return run_db(read)


def seed_conversation(product_id: uuid.UUID, phone: str = PHONE) -> tuple[uuid.UUID, uuid.UUID]:
    async def create(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
        lead = Lead(
            product_id=product_id,
            phone_e164=phone,
            full_name="Asha Rao",
            company="Northwind",
            status=LeadStatus.QUALIFIED,
        )
        session.add(lead)
        await session.flush()
        conversation = Conversation(
            lead_id=lead.id, product_id=product_id, status=ConversationStatus.ACTIVE
        )
        session.add(conversation)
        await session.flush()
        return lead.id, conversation.id

    return run_db(create)


def run_tool(
    name: str,
    arguments: dict[str, Any],
    *,
    conversation: tuple[uuid.UUID, uuid.UUID],
    calendar: FakeCalendarProvider | None,
    policy: SlotPolicy | None = None,
) -> ai_tools.ToolResult:
    """Run a tool with the calendar the caller resolved, as the turn handler does."""
    lead_id, conversation_id = conversation

    async def call(session: AsyncSession) -> ai_tools.ToolResult:
        lead = await conversation_repo.get_lead(session, lead_id)
        row = await conversation_repo.get_conversation(session, conversation_id)
        assert lead is not None and row is not None
        context = ai_tools.ToolContext(
            session=session,
            lead=lead,
            conversation=row,
            slot_policy=(policy or a_policy()) if calendar is not None else None,
            calendar=calendar,
            qualification_rules=QualificationRules(),
            now=NOW,
        )
        return await ai_tools.dispatch(name, arguments, context)

    return run_db(call)


def book(
    conversation: tuple[uuid.UUID, uuid.UUID],
    calendar: FakeCalendarProvider | None,
    start: datetime | str = TUESDAY_10AM,
    **extra: Any,
) -> ai_tools.ToolResult:
    start_time = start if isinstance(start, str) else start.isoformat()
    return run_tool(
        "book_demo",
        {"start_time": start_time, **extra},
        conversation=conversation,
        calendar=calendar,
    )


def first_slot(conversation: tuple[uuid.UUID, uuid.UUID], calendar: FakeCalendarProvider) -> str:
    result = run_tool("get_available_slots", {}, conversation=conversation, calendar=calendar)
    assert result.ok, result.error
    return str(result.data["slots"][0]["start_time"])


def booking_rows() -> list[Booking]:
    async def read(session: AsyncSession) -> list[Booking]:
        return list((await session.execute(select(Booking))).scalars().all())

    return run_db(read)


def confirmed_products_at(start: datetime) -> list[uuid.UUID]:
    return sorted(
        (
            row.product_id
            for row in booking_rows()
            if row.status is BookingStatus.CONFIRMED and row.start_time == start
        ),
        key=str,
    )


def insert_confirmed(product_id: uuid.UUID, lead_id: uuid.UUID, start: datetime) -> None:
    async def insert(session: AsyncSession) -> None:
        session.add(
            Booking(
                product_id=product_id,
                lead_id=lead_id,
                calendar_event_id=f"demo-{uuid.uuid4()}",
                start_time=start,
                end_time=start + HALF_HOUR,
                timezone="Asia/Kolkata",
                status=BookingStatus.CONFIRMED,
            )
        )
        await session.flush()

    run_db(insert)


def google_settings() -> Settings:
    """The approved setup: one operations account's OAuth client and refresh token."""
    return Settings(
        calendar_provider="google",
        google_secret_manager_project="ops-project",
        google_calendar_token_secret_name="shared-refresh-token",
        google_oauth_client_id="shared-client",
        google_oauth_client_secret="shared-secret",  # noqa: S106 - test fixture
    )


def google_for(product: Product) -> GoogleCalendarProvider:
    provider = build_calendar_provider(google_settings(), product=product)
    assert isinstance(provider, GoogleCalendarProvider)
    return provider


def unsaved(calendar_id: str | None, **overrides: Any) -> Product:
    return Product(
        slug="k-unsaved", display_name="Unsaved", google_calendar_id=calendar_id, **overrides
    )


# ---------------------------------------------------------------- the same hour


class TestTheSameHourInDifferentProducts:
    def test_every_product_books_the_same_hour_on_its_own_calendar(self) -> None:
        """The requirement in one line: three products, Tuesday 10:00, three bookings.

        The same person, even — one lead per product ([D41]) — because nothing about the
        customer should make the calendars share an hour.
        """
        products = [
            make_product(slug, f"{slug}@calendar.example")
            for slug in ("k-first", "k-second", "k-third")
        ]
        calendars = {
            product: FakeCalendarProvider(calendar_id=f"{slug}@calendar.example")
            for product, slug in zip(products, ("k-first", "k-second", "k-third"), strict=True)
        }

        for product in products:
            result = book(seed_conversation(product), calendars[product])
            assert result.ok, result.error

        assert confirmed_products_at(TUESDAY_10AM) == sorted(products, key=str)
        assert all(calendar.event_count == 1 for calendar in calendars.values())

    def test_the_database_allows_it(self) -> None:
        """Not only the tool path: the confirmed-slot index itself is per product."""
        first = make_product("k-first", "first@calendar.example")
        second = make_product("k-second", "second@calendar.example")
        first_lead, _ = seed_conversation(first)
        second_lead, _ = seed_conversation(second)

        insert_confirmed(first, first_lead, TUESDAY_10AM)
        insert_confirmed(second, second_lead, TUESDAY_10AM)

        assert len(confirmed_products_at(TUESDAY_10AM)) == 2


class TestTheSameHourInOneProduct:
    def test_a_second_customer_cannot_take_a_confirmed_hour(self) -> None:
        """Existing double-booking protection, unchanged within a product: the re-check
        against the product's calendar finds the first booking's event."""
        product = make_product("k-only", "only@calendar.example")
        calendar = FakeCalendarProvider(calendar_id="only@calendar.example")

        assert book(seed_conversation(product, "+919876500711"), calendar).ok
        second = book(seed_conversation(product, "+919876500712"), calendar)

        assert second.ok is False
        assert "no longer available" in (second.error or "")
        assert confirmed_products_at(TUESDAY_10AM) == [product]
        assert calendar.event_count == 1

    def test_the_database_refuses_a_second_confirmation(self) -> None:
        """The race §12 requires closing: two confirmations of one hour in one product
        cannot both commit, whatever the re-check saw."""
        product = make_product("k-only", "only@calendar.example")
        first_lead, _ = seed_conversation(product, "+919876500711")
        second_lead, _ = seed_conversation(product, "+919876500712")

        insert_confirmed(product, first_lead, TUESDAY_10AM)
        with pytest.raises(IntegrityError):
            insert_confirmed(product, second_lead, TUESDAY_10AM)


class TestOneCalendarOneProduct:
    def test_two_active_products_cannot_share_a_calendar(self) -> None:
        """What keeps "per product" equal to "per calendar". Two products on one calendar
        could each confirm the same hour on it."""
        make_product("k-first", "shared@calendar.example")

        with pytest.raises(IntegrityError):
            make_product("k-second", "shared@calendar.example")

    def test_a_retired_product_may_keep_its_calendar_recorded(self) -> None:
        make_product("k-retired", "shared@calendar.example", active=False)
        make_product("k-current", "shared@calendar.example")


# ---------------------------------------------------------------- provider selection


class TestProviderSelection:
    def test_each_product_gets_a_provider_for_its_own_calendar(self) -> None:
        alpha = google_for(unsaved("alpha@calendar.example"))
        beta = google_for(unsaved("beta@calendar.example"))

        assert alpha._calendar_id == "alpha@calendar.example"  # noqa: SLF001
        assert beta._calendar_id == "beta@calendar.example"  # noqa: SLF001
        assert alpha is not beta

    def test_building_one_product_does_not_change_another(self) -> None:
        """No cache and no shared mutable configuration. If one is ever added, this is
        what catches the second product being handed the first one's calendar."""
        alpha = google_for(unsaved("alpha@calendar.example"))
        google_for(unsaved("beta@calendar.example", demo_timezone="Europe/London"))

        assert alpha._calendar_id == "alpha@calendar.example"  # noqa: SLF001
        assert alpha._timezone == Settings().demo_timezone  # noqa: SLF001

    def test_the_oauth_identity_is_shared_but_a_product_may_name_its_own_token(self) -> None:
        """[D43]: one operations account serves every calendar, so the client and token
        are shared. A product in a different Google account names a different token
        *secret* — never a token value, and never a second OAuth client.

        The credential now lives in an authenticator ([D62]) rather than on the provider,
        because how the platform proves its identity is not the calendar's business.
        """
        shared = google_for(unsaved("alpha@calendar.example"))
        separate = google_for(
            unsaved(
                "herba@calendar.example", google_calendar_token_secret_name="herba-refresh-token"
            )
        )

        assert shared._auth._client_id == separate._auth._client_id  # noqa: SLF001
        assert shared._auth._client_id == "shared-client"  # noqa: SLF001
        assert shared._auth._refresh_token_secret_name == "shared-refresh-token"  # noqa: SLF001
        assert separate._auth._refresh_token_secret_name == "herba-refresh-token"  # noqa: SLF001
        # Built lazily: nothing read a secret to construct either provider.
        assert shared._auth._refresh_token is None  # noqa: SLF001

    def test_service_account_mode_shares_one_identity_and_keeps_own_calendars(self) -> None:
        """[D62]: the worker authenticates as itself, so the identity is the running
        service and every product's calendar is shared with it. Sharing one identity must
        not merge two calendars — that is what makes per-product booking correct.
        """
        from app.integrations.calendar.google_auth import ServiceAccountAuthenticator

        settings = Settings(calendar_provider="google", google_calendar_auth_mode="service_account")
        alpha = build_calendar_provider(settings, product=unsaved("alpha@calendar.example"))
        beta = build_calendar_provider(settings, product=unsaved("beta@calendar.example"))

        assert isinstance(alpha, GoogleCalendarProvider)
        assert isinstance(beta, GoogleCalendarProvider)
        # Built with no OAuth client and no secret to read, because neither is used.
        assert isinstance(alpha._auth, ServiceAccountAuthenticator)  # noqa: SLF001
        assert alpha._calendar_id == "alpha@calendar.example"  # noqa: SLF001
        assert beta._calendar_id == "beta@calendar.example"  # noqa: SLF001

    def test_the_timezone_follows_the_products_slot_policy(self) -> None:
        provider = google_for(unsaved("london@calendar.example", demo_timezone="Europe/London"))

        assert provider._timezone == "Europe/London"  # noqa: SLF001

    def test_a_conversation_is_given_its_own_products_calendar(self) -> None:
        """End to end through the turn handler's resolution, against stored products."""
        alpha = load_product(make_product("k-alpha", "alpha@calendar.example"))
        beta = load_product(make_product("k-beta", "beta@calendar.example"))

        _, alpha_calendar = resolve_booking_capability(Settings(), alpha)
        _, beta_calendar = resolve_booking_capability(Settings(), beta)

        assert isinstance(alpha_calendar, FakeCalendarProvider)
        assert isinstance(beta_calendar, FakeCalendarProvider)
        assert alpha_calendar.calendar_id == "alpha@calendar.example"
        assert beta_calendar.calendar_id == "beta@calendar.example"


# ---------------------------------------------------------------- availability


class TestAvailabilityIsolation:
    def test_busy_time_on_one_calendar_does_not_make_another_busy(self) -> None:
        alpha = make_product("k-alpha", "alpha@calendar.example")
        beta = make_product("k-beta", "beta@calendar.example")
        alpha_conversation = seed_conversation(alpha)
        beta_conversation = seed_conversation(beta)
        alpha_calendar = FakeCalendarProvider(calendar_id="alpha@calendar.example")
        beta_calendar = FakeCalendarProvider(calendar_id="beta@calendar.example")

        hour = first_slot(beta_conversation, beta_calendar)
        start = datetime.fromisoformat(hour)
        alpha_calendar.mark_busy(TimeInterval(start=start, end=start + HALF_HOUR))

        assert first_slot(alpha_conversation, alpha_calendar) != hour
        assert first_slot(beta_conversation, beta_calendar) == hour

    def test_a_booking_in_one_product_leaves_the_hour_free_in_another(self) -> None:
        alpha = make_product("k-alpha", "alpha@calendar.example")
        beta = make_product("k-beta", "beta@calendar.example")
        alpha_conversation = seed_conversation(alpha)
        beta_conversation = seed_conversation(beta)
        alpha_calendar = FakeCalendarProvider(calendar_id="alpha@calendar.example")
        beta_calendar = FakeCalendarProvider(calendar_id="beta@calendar.example")

        hour = first_slot(alpha_conversation, alpha_calendar)
        assert book(alpha_conversation, alpha_calendar, hour).ok

        assert first_slot(alpha_conversation, alpha_calendar) != hour
        assert first_slot(beta_conversation, beta_calendar) == hour


# ---------------------------------------------------------------- wrong-calendar defense


class TestWrongCalendarDefense:
    def test_no_tool_accepts_a_product_or_a_calendar(self) -> None:
        """The model cannot name a product or a calendar because no tool has a parameter
        for either. The context supplies both, from the conversation."""
        for spec in ai_tools.tool_specs():
            properties = spec.parameters.get("properties", {})
            assert not [name for name in properties if "product" in name or "calendar" in name], (
                f"{spec.name} exposes {list(properties)}"
            )

    def test_extra_arguments_cannot_redirect_a_booking(self) -> None:
        """A model that invents `calendar_id` and `product_id` arguments still books into
        its own conversation's product, on that product's calendar."""
        alpha = make_product("k-alpha", "alpha@calendar.example")
        beta = make_product("k-beta", "beta@calendar.example")
        alpha_calendar = FakeCalendarProvider(calendar_id="alpha@calendar.example")
        beta_calendar = FakeCalendarProvider(calendar_id="beta@calendar.example")

        result = book(
            seed_conversation(alpha),
            alpha_calendar,
            calendar_id="beta@calendar.example",
            product_id=str(beta),
        )

        assert result.ok, result.error
        assert alpha_calendar.event_count == 1
        assert beta_calendar.event_count == 0
        assert [row.product_id for row in booking_rows()] == [alpha]


# ---------------------------------------------------------------- missing calendar


class TestMissingCalendar:
    @pytest.mark.parametrize("settings_factory", [Settings, google_settings])
    def test_the_registry_refuses_a_product_without_a_calendar(self, settings_factory: Any) -> None:
        """The fake refuses exactly as the real provider does, so the rule is exercised
        by the suite rather than only in production."""
        with pytest.raises(CalendarNotConfiguredError, match="no calendar"):
            build_calendar_provider(settings_factory(), product=unsaved(None))

    def test_no_other_calendar_stands_in(self) -> None:
        """Another product has a calendar; it is not borrowed, and there is no global one."""
        make_product("k-alpha", "alpha@calendar.example")
        bare = load_product(make_product("k-bare", None))

        assert resolve_booking_capability(Settings(), bare) == (None, None)

    def test_the_booking_tools_refuse_and_nothing_is_booked(self) -> None:
        bare = make_product("k-bare", None)
        conversation = seed_conversation(bare)

        slots = run_tool("get_available_slots", {}, conversation=conversation, calendar=None)
        booked = book(conversation, None)

        assert slots.ok is False
        assert booked.ok is False
        assert "not available" in (booked.error or "")
        assert booking_rows() == []


# ---------------------------------------------------------------- slot policy


class TestSlotPolicyInheritance:
    def test_a_product_override_changes_only_that_field(self) -> None:
        product = load_product(
            make_product("k-long", "long@calendar.example", demo_duration_minutes=60)
        )
        platform = policy_from_settings(Settings())

        policy, _calendar = resolve_booking_capability(Settings(), product)

        assert policy is not None
        assert policy.duration_minutes == 60
        assert policy.buffer_minutes == platform.buffer_minutes
        assert policy.timezone == platform.timezone
        assert policy.working_days == platform.working_days

    def test_an_invalid_override_makes_booking_unavailable_rather_than_falling_back(
        self,
    ) -> None:
        """A 30-minute demo cannot fit a 15-minute day. The platform's hours are not
        quietly used instead — the product's own setting is wrong, and says so."""
        product = load_product(
            make_product(
                "k-broken",
                "broken@calendar.example",
                demo_working_hours_start="10:00",
                demo_working_hours_end="10:15",
            )
        )

        assert resolve_booking_capability(Settings(), product) == (None, None)


# ---------------------------------------------------------------- [D54]


class TestExistingBehaviourIsUnchanged:
    def test_a_calendar_and_no_overrides_books_exactly_as_before(self) -> None:
        """Academicae's case, without depending on its identity: once a product's calendar
        is supplied through the product row, it books on the platform's slot policy
        exactly as the single-product platform did."""
        product = make_product("k-existing", "existing@calendar.example")
        policy, calendar = resolve_booking_capability(Settings(), load_product(product))

        assert policy == policy_from_settings(Settings())
        assert isinstance(calendar, FakeCalendarProvider)

        conversation = seed_conversation(product)
        slots = run_tool(
            "get_available_slots", {}, conversation=conversation, calendar=calendar, policy=policy
        )
        assert slots.ok, slots.error
        booked = run_tool(
            "book_demo",
            {"start_time": slots.data["slots"][0]["start_time"]},
            conversation=conversation,
            calendar=calendar,
            policy=policy,
        )

        assert booked.ok, booked.error
        [row] = booking_rows()
        assert row.product_id == product
        assert row.status is BookingStatus.CONFIRMED
        assert row.timezone == Settings().demo_timezone
        assert row.end_time - row.start_time == timedelta(minutes=Settings().demo_duration_minutes)


# ---------------------------------------------------------------- notifications


class TestNotificationsNameTheProduct:
    def test_a_booking_notification_carries_and_names_its_product(self) -> None:
        """[D47]. Everyone is still told ([D3]); each row now says which product, both as
        data and in the headline a salesperson reads."""
        from app.core.security import hash_password

        async def users(session: AsyncSession) -> None:
            for index, role in enumerate((UserRole.ADMIN, UserRole.SALESPERSON)):
                session.add(
                    User(
                        email=f"k-user{index}@example.com",
                        password_hash=hash_password("a-sufficiently-long-password"),
                        role=role,
                    )
                )

        run_db(users)
        product = make_product("k-named", "named@calendar.example")
        assert book(seed_conversation(product), FakeCalendarProvider()).ok
        [booking] = booking_rows()

        run_notify(str(booking.id))

        async def read(session: AsyncSession) -> list[Notification]:
            return list((await session.execute(select(Notification))).scalars().all())

        rows = run_db(read)
        assert len(rows) == 2
        assert all(row.product_id == product for row in rows)
        assert all(row.title == "Demo booked — K-NAMED" for row in rows)
        assert all("Asha Rao" in row.message for row in rows)


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


class TestInactiveProduct:
    def test_an_inactive_product_takes_no_bookings_even_with_a_calendar(self) -> None:
        """Checked before the calendar: a deactivated product with a working calendar still
        must not commit anyone's time (Phase 26)."""
        product = load_product(make_product("k-retired", "retired@calendar.example", active=False))

        assert resolve_booking_capability(Settings(), product) == (None, None)
