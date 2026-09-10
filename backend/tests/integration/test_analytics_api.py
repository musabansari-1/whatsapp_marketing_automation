"""Overview metric tests (specification §17).

The definitions are the point. "Sent", "replies" and "interested" each involve a
judgement about which rows answer the question, and a dashboard that counts the wrong
ones is worse than no dashboard: it looks authoritative while being wrong.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import (
    BookingStatus,
    CampaignStatus,
    LeadStatus,
    MessageDirection,
    MessageStatus,
    NotificationType,
)
from app.persistence.models import (
    Audience,
    Booking,
    Campaign,
    Conversation,
    ConversationMessage,
    Lead,
    Message,
    Notification,
    WhatsAppTemplate,
)
from tests.integration.conftest import ADMIN_EMAIL, SALES_EMAIL, login
from tests.integration.helpers import ensure_product_id, run_db

pytestmark = pytest.mark.integration

START = datetime(2026, 9, 2, 10, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def clean(engine: Any) -> Iterator[None]:
    async def clear(session: AsyncSession) -> None:
        await session.execute(delete(Notification))
        await session.execute(delete(Booking))
        await session.execute(delete(ConversationMessage))
        await session.execute(delete(Message))
        await session.execute(delete(Conversation))
        await session.execute(delete(Campaign))
        await session.execute(delete(Lead))
        await session.execute(delete(Audience))
        await session.execute(delete(WhatsAppTemplate))

    run_db(clear)
    yield
    run_db(clear)


def seed_lead(phone: str, status: LeadStatus = LeadStatus.NEW) -> str:
    async def create(session: AsyncSession) -> str:
        lead = Lead(
            phone_e164=phone,
            full_name="Asha",
            status=status,
            product_id=await ensure_product_id(session),
        )
        session.add(lead)
        await session.flush()
        return str(lead.id)

    return run_db(create)


def seed_message(
    lead_id: str,
    *,
    direction: MessageDirection = MessageDirection.OUTBOUND,
    status: MessageStatus = MessageStatus.SENT,
) -> None:
    async def create(session: AsyncSession) -> None:
        session.add(
            Message(
                lead_id=uuid.UUID(lead_id),
                product_id=await ensure_product_id(session),
                direction=direction,
                status=status,
                phone_e164="+919876500400",
                provider_message_id=f"wamid-{uuid.uuid4()}",
                body="hello",
            )
        )

    run_db(create)


def overview(client: TestClient) -> dict[str, Any]:
    response = client.get("/api/v1/analytics/overview")
    assert response.status_code == 200, response.text
    return dict(response.json())


class TestAuthentication:
    def test_a_session_is_required(self, client: TestClient) -> None:
        assert client.get("/api/v1/analytics/overview").status_code == 401
        assert client.get("/api/v1/analytics/leads-by-status").status_code == 401

    def test_a_salesperson_may_read_the_overview(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        """§17's dashboard is what a salesperson uses to monitor the lifecycle."""
        login(client, email=SALES_EMAIL)
        assert client.get("/api/v1/analytics/overview").status_code == 200


class TestEmptyState:
    def test_everything_is_zero_before_anything_happens(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        """A fresh install must read as empty, not as broken."""
        login(client)
        body = overview(client)

        assert body["campaigns"] == 0
        assert body["messages_sent"] == 0
        assert body["replies"] == 0
        assert body["demos_booked"] == 0


class TestDeliveryMetrics:
    def test_sent_includes_delivered_and_read(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        """Cumulative, or "sent" would fall as delivery receipts arrive — which reads
        as messages being un-sent."""
        login(client)
        lead_id = seed_lead("+919876500401")
        seed_message(lead_id, status=MessageStatus.SENT)
        seed_message(lead_id, status=MessageStatus.DELIVERED)
        seed_message(lead_id, status=MessageStatus.READ)

        body = overview(client)

        assert body["messages_sent"] == 3
        assert body["delivered"] == 2
        assert body["read"] == 1

    def test_failed_messages_are_not_counted_as_sent(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        login(client)
        lead_id = seed_lead("+919876500402")
        seed_message(lead_id, status=MessageStatus.FAILED)

        body = overview(client)

        assert body["messages_sent"] == 0
        assert body["failed"] == 1

    def test_inbound_messages_are_not_counted_as_sent(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        """Otherwise a busy conversation would inflate reach."""
        login(client)
        lead_id = seed_lead("+919876500403")
        seed_message(lead_id, direction=MessageDirection.INBOUND, status=MessageStatus.DELIVERED)

        assert overview(client)["messages_sent"] == 0


class TestReplies:
    def test_replies_count_people_not_messages(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        """A customer who sends five messages is one reply to a salesperson reading
        this. Counting messages would make engagement look like reach."""
        login(client)
        lead_id = seed_lead("+919876500404")
        for _ in range(5):
            seed_message(
                lead_id, direction=MessageDirection.INBOUND, status=MessageStatus.DELIVERED
            )

        assert overview(client)["replies"] == 1

    def test_each_replying_customer_counts_once(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        login(client)
        for index in range(3):
            lead_id = seed_lead(f"+91987650041{index}")
            seed_message(
                lead_id, direction=MessageDirection.INBOUND, status=MessageStatus.DELIVERED
            )

        assert overview(client)["replies"] == 3


class TestFunnelMetrics:
    def test_interested_includes_everyone_past_it(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        """A qualified lead is by definition interested. If "interested" fell as leads
        progressed, the dashboard would read as leads being lost."""
        login(client)
        seed_lead("+919876500420", LeadStatus.INTERESTED)
        seed_lead("+919876500421", LeadStatus.QUALIFYING)
        seed_lead("+919876500422", LeadStatus.QUALIFIED)
        seed_lead("+919876500423", LeadStatus.DEMO_BOOKED)

        assert overview(client)["interested"] == 4

    def test_qualified_includes_demo_states(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        login(client)
        seed_lead("+919876500424", LeadStatus.QUALIFIED)
        seed_lead("+919876500425", LeadStatus.DEMO_BOOKED)
        seed_lead("+919876500426", LeadStatus.RESPONDED)

        assert overview(client)["qualified"] == 2

    def test_uninterested_leads_are_excluded(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        login(client)
        seed_lead("+919876500427", LeadStatus.NOT_INTERESTED)
        seed_lead("+919876500428", LeadStatus.OPTED_OUT)

        body = overview(client)

        assert body["interested"] == 0
        assert body["total_leads"] == 2


class TestBookingMetrics:
    def test_only_confirmed_demos_are_counted(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        """A cancelled or failed attempt is not a demo anyone will attend."""
        login(client)
        lead_id = seed_lead("+919876500430")

        async def create(session: AsyncSession) -> None:
            for offset, status in enumerate(
                [BookingStatus.CONFIRMED, BookingStatus.CANCELLED, BookingStatus.FAILED]
            ):
                start = START + timedelta(hours=offset)
                session.add(
                    Booking(
                        lead_id=uuid.UUID(lead_id),
                        calendar_event_id=f"demo-{uuid.uuid4()}",
                        start_time=start,
                        end_time=start + timedelta(minutes=30),
                        timezone="Asia/Kolkata",
                        status=status,
                    )
                )

        run_db(create)

        assert overview(client)["demos_booked"] == 1


class TestNotificationCount:
    def test_the_unread_count_is_the_callers_own(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        """[D3] gives every user their own rows, so a shared number would be
        meaningless."""
        lead_id = seed_lead("+919876500440")

        async def notify(session: AsyncSession) -> None:
            from sqlalchemy import select

            from app.persistence.models import User

            result = await session.execute(select(User).where(User.email == SALES_EMAIL))
            user = result.scalars().one()
            session.add(
                Notification(
                    user_id=user.id,
                    type=NotificationType.DEMO_BOOKED,
                    title="Demo booked",
                    message="Asha booked a demo.",
                    related_entity_type="booking",
                    related_entity_id=uuid.UUID(lead_id),
                )
            )

        run_db(notify)

        login(client, email=ADMIN_EMAIL)
        assert overview(client)["unread_notifications"] == 0

        client.post("/api/v1/auth/logout")
        login(client, email=SALES_EMAIL)
        assert overview(client)["unread_notifications"] == 1


class TestLeadFunnel:
    def test_every_state_appears_including_empty_ones(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        """A funnel with gaps hides where leads are stalling, which is what the view
        is for."""
        login(client)
        seed_lead("+919876500450", LeadStatus.QUALIFIED)

        counts = client.get("/api/v1/analytics/leads-by-status").json()["counts"]

        assert counts["QUALIFIED"] == 1
        assert counts["NEW"] == 0
        assert len(counts) == len(LeadStatus)


class TestCampaignMetrics:
    def test_campaigns_in_flight_are_reported(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        login(client)

        async def create(session: AsyncSession) -> None:
            audience = Audience(
                name="Batch",
                status="CONFIRMED",
                product_id=await ensure_product_id(session),
            )
            template = WhatsAppTemplate(
                name="promo",
                language="en",
                # Meta's own ID; required, because [D6] means a template only exists
                # here because Meta reported it.
                provider_template_id=f"tpl-{uuid.uuid4()}",
                category="MARKETING",
                provider_status="APPROVED",
                variable_count=1,
            )
            session.add_all([audience, template])
            await session.flush()

            for status in (CampaignStatus.PROCESSING, CampaignStatus.COMPLETED):
                session.add(
                    Campaign(
                        name=f"Campaign {status.value}",
                        audience_id=audience.id,
                        # As `campaign_repo.create` does: a campaign takes its product
                        # from its audience and never chooses one (§28.6).
                        product_id=audience.product_id,
                        template_id=template.id,
                        status=status,
                    )
                )

        run_db(create)
        body = overview(client)

        assert body["campaigns"] == 2
        assert body["campaigns_sending"] == 1
