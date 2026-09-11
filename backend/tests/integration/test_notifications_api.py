"""Notification and booking endpoint tests (§13, §16, §17, [D3]).

The isolation between users is the point: a notification is personal, and no route here
takes a user ID. One salesperson must not be able to read or clear another's.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import BookingStatus, NotificationType
from app.persistence.models import Booking, Conversation, Lead, Notification, User
from app.persistence.repositories import bookings as booking_repo
from tests.integration.conftest import ADMIN_EMAIL, SALES_EMAIL, login
from tests.integration.helpers import ensure_product_id, run_db

pytestmark = pytest.mark.integration

START = datetime(2026, 9, 2, 10, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def clean_bookings(engine: Any) -> Iterator[None]:
    async def clear(session: AsyncSession) -> None:
        await session.execute(delete(Notification))
        await session.execute(delete(Booking))
        await session.execute(delete(Conversation))
        await session.execute(delete(Lead))

    run_db(clear)
    yield
    run_db(clear)


def seed_lead(phone: str = "+919876500300") -> str:
    async def create(session: AsyncSession) -> str:
        lead = Lead(
            phone_e164=phone, full_name="Asha Rao", product_id=await ensure_product_id(session)
        )
        session.add(lead)
        await session.flush()
        return str(lead.id)

    return run_db(create)


_slot_offsets = iter(range(1, 500))


def seed_booking(
    lead_id: str,
    *,
    start: datetime | None = None,
    status: BookingStatus = BookingStatus.CONFIRMED,
) -> str:
    """Seed a booking in its own slot.

    Distinct start times by default because `uq_bookings_product_confirmed_start_time`
    forbids two confirmed bookings at the same moment in one product — the constraint that closes §12's race.
    """
    if start is None:
        start = START + timedelta(hours=next(_slot_offsets))

    async def create(session: AsyncSession) -> str:
        booking = Booking(
            product_id=await ensure_product_id(session),
            lead_id=uuid.UUID(lead_id),
            calendar_event_id=f"demo-{uuid.uuid4()}",
            start_time=start,
            end_time=start + timedelta(minutes=30),
            timezone="Asia/Kolkata",
            status=status,
            confirmed_at=datetime.now(UTC) if status is BookingStatus.CONFIRMED else None,
        )
        session.add(booking)
        await session.flush()
        return str(booking.id)

    return run_db(create)


def notify_everyone(booking_id: str, *, title: str = "Demo booked") -> int:
    async def fan_out(session: AsyncSession) -> int:
        return await booking_repo.fan_out_notification(
            session,
            notification_type=NotificationType.DEMO_BOOKED,
            title=title,
            message="Asha Rao booked a demo.",
            entity_type=booking_repo.ENTITY_BOOKING,
            entity_id=uuid.UUID(booking_id),
            product_id=await ensure_product_id(session),
        )

    return run_db(fan_out)


def user_id(email: str) -> str:
    async def fetch(session: AsyncSession) -> str:
        result = await session.execute(select(User.id).where(User.email == email))
        return str(result.scalars().one())

    return run_db(fetch)


class TestAuthentication:
    def test_every_endpoint_requires_a_session(self, client: TestClient) -> None:
        assert client.get("/api/v1/notifications").status_code == 401
        assert client.post("/api/v1/notifications/read-all").status_code == 401
        assert client.get("/api/v1/bookings").status_code == 401
        assert client.get(f"/api/v1/bookings/{uuid.uuid4()}").status_code == 401

    def test_a_salesperson_may_read_bookings(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        """§9 restricts broadcasting, not reading — and a salesperson about to run a
        demo needs to see it."""
        login(client, email=SALES_EMAIL)
        assert client.get("/api/v1/bookings").status_code == 200


class TestNotificationListing:
    def test_a_user_sees_their_own_notifications(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        login(client)
        booking_id = seed_booking(seed_lead())
        notify_everyone(booking_id)

        body = client.get("/api/v1/notifications").json()

        assert body["total"] == 1
        assert body["items"][0]["related_entity_id"] == booking_id

    def test_the_unread_count_is_reported(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        """The bell badge shows it regardless of which page is being viewed."""
        login(client)
        notify_everyone(seed_booking(seed_lead("+919876500301")))
        notify_everyone(seed_booking(seed_lead("+919876500302")))

        body = client.get("/api/v1/notifications?limit=1").json()

        assert len(body["items"]) == 1
        assert body["unread"] == 2

    def test_unread_only_filters(self, client: TestClient, seeded_users: dict[str, str]) -> None:
        login(client)
        first = seed_booking(seed_lead("+919876500303"))
        notify_everyone(first)
        notify_everyone(seed_booking(seed_lead("+919876500304")))

        mine = client.get("/api/v1/notifications").json()["items"]
        client.post(f"/api/v1/notifications/{mine[0]['id']}/read")

        body = client.get("/api/v1/notifications?unread_only=true").json()
        assert body["total"] == 1

    def test_one_user_cannot_see_anothers(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        """[D3] gives everyone their own row. They are not shared objects."""
        notify_everyone(seed_booking(seed_lead()))

        login(client, email=SALES_EMAIL)
        sales_ids = {item["id"] for item in client.get("/api/v1/notifications").json()["items"]}

        client.post("/api/v1/auth/logout")
        login(client, email=ADMIN_EMAIL)
        admin_ids = {item["id"] for item in client.get("/api/v1/notifications").json()["items"]}

        assert sales_ids and admin_ids
        assert sales_ids.isdisjoint(admin_ids)


class TestMarkingRead:
    def test_a_notification_can_be_marked_read(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        login(client)
        notify_everyone(seed_booking(seed_lead()))
        notification = client.get("/api/v1/notifications").json()["items"][0]

        response = client.post(f"/api/v1/notifications/{notification['id']}/read")

        assert response.status_code == 200
        assert response.json()["is_read"] is True
        assert response.json()["read_at"]

    def test_marking_twice_is_harmless(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        """A double-clicked bell must not become an error, and must not move the
        timestamp."""
        login(client)
        notify_everyone(seed_booking(seed_lead()))
        notification = client.get("/api/v1/notifications").json()["items"][0]

        first = client.post(f"/api/v1/notifications/{notification['id']}/read").json()
        second = client.post(f"/api/v1/notifications/{notification['id']}/read").json()

        assert second["read_at"] == first["read_at"]

    def test_another_users_notification_reads_as_missing(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        """404 rather than 403: confirming it exists would leak that another user was
        notified."""
        notify_everyone(seed_booking(seed_lead()))

        login(client, email=SALES_EMAIL)
        theirs = client.get("/api/v1/notifications").json()["items"][0]["id"]
        client.post("/api/v1/auth/logout")

        login(client, email=ADMIN_EMAIL)
        assert client.post(f"/api/v1/notifications/{theirs}/read").status_code == 404

    def test_a_missing_notification_is_404(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        login(client)
        assert client.post(f"/api/v1/notifications/{uuid.uuid4()}/read").status_code == 404

    def test_read_all_clears_the_badge(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        login(client)
        notify_everyone(seed_booking(seed_lead("+919876500310")))
        notify_everyone(seed_booking(seed_lead("+919876500311")))

        response = client.post("/api/v1/notifications/read-all")

        assert response.json()["marked_read"] == 2
        assert client.get("/api/v1/notifications").json()["unread"] == 0

    def test_read_all_leaves_other_users_alone(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        notify_everyone(seed_booking(seed_lead()))

        login(client, email=ADMIN_EMAIL)
        client.post("/api/v1/notifications/read-all")
        client.post("/api/v1/auth/logout")

        login(client, email=SALES_EMAIL)
        assert client.get("/api/v1/notifications").json()["unread"] == 1

    def test_read_all_with_nothing_unread_is_fine(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        login(client)
        assert client.post("/api/v1/notifications/read-all").json()["marked_read"] == 0


class TestBookings:
    def test_bookings_are_listed(self, client: TestClient, seeded_users: dict[str, str]) -> None:
        login(client)
        booking_id = seed_booking(seed_lead())

        body = client.get("/api/v1/bookings").json()

        assert body["total"] == 1
        assert body["items"][0]["id"] == booking_id

    def test_bookings_can_be_filtered_by_status(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        login(client)
        lead_id = seed_lead()
        seed_booking(lead_id, status=BookingStatus.CONFIRMED)
        seed_booking(lead_id, status=BookingStatus.CANCELLED)

        body = client.get("/api/v1/bookings?status=CANCELLED").json()

        assert body["total"] == 1
        assert body["items"][0]["status"] == "CANCELLED"

    def test_a_booking_carries_its_own_timezone(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        """[D8]: stored per booking, so a policy change cannot reinterpret it."""
        login(client)
        booking_id = seed_booking(seed_lead())

        body = client.get(f"/api/v1/bookings/{booking_id}").json()
        assert body["timezone"] == "Asia/Kolkata"

    def test_a_missing_booking_is_404(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        login(client)
        assert client.get(f"/api/v1/bookings/{uuid.uuid4()}").status_code == 404


class TestNoManualBooking:
    def test_bookings_cannot_be_created_over_http(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        """§12 puts booking behind the recheck and Calendar confirmation. An endpoint
        that wrote a booking row directly would be a way to record a demo that does
        not exist in the calendar."""
        login(client)

        response = client.post(
            "/api/v1/bookings",
            json={"lead_id": str(uuid.uuid4()), "start_time": START.isoformat()},
        )
        assert response.status_code in (404, 405)
