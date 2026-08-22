"""Lead and conversation endpoint tests (§9, §10, §16, §17).

Drives the endpoints through the app, so authentication, filtering and the §10
transition rules are exercised as a client meets them.
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

from app.domain.enums import ConversationStatus, LeadStatus
from app.persistence.models import (
    AIRun,
    Conversation,
    ConversationMessage,
    Lead,
    Message,
    WhatsAppSuppression,
)
from tests.integration.conftest import SALES_EMAIL, login
from tests.integration.helpers import run_db

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def clean_conversations(engine: Any) -> Iterator[None]:
    async def clear(session: AsyncSession) -> None:
        await session.execute(delete(AIRun))
        await session.execute(delete(ConversationMessage))
        await session.execute(delete(Message))
        await session.execute(delete(Conversation))
        await session.execute(delete(WhatsAppSuppression))
        await session.execute(delete(Lead))

    run_db(clear)
    yield
    run_db(clear)


def seed_lead(
    *,
    phone: str = "+919876500010",
    name: str = "Asha Rao",
    company: str | None = "Northwind",
    status: LeadStatus = LeadStatus.RESPONDED,
) -> str:
    async def seed(session: AsyncSession) -> str:
        lead = Lead(phone_e164=phone, full_name=name, company=company, status=status)
        session.add(lead)
        await session.flush()
        return str(lead.id)

    return run_db(seed)


def seed_conversation(
    lead_id: str,
    *,
    status: ConversationStatus = ConversationStatus.WAITING_FOR_USER,
    last_inbound_age: timedelta | None = timedelta(minutes=5),
    turns: tuple[tuple[str, str], ...] = (("user", "Hello"), ("assistant", "Hi there")),
) -> str:
    async def seed(session: AsyncSession) -> str:
        conversation = Conversation(
            lead_id=uuid.UUID(lead_id),
            status=status,
            last_inbound_at=(
                None if last_inbound_age is None else datetime.now(UTC) - last_inbound_age
            ),
        )
        session.add(conversation)
        await session.flush()

        for role, content in turns:
            session.add(
                ConversationMessage(conversation_id=conversation.id, role=role, content=content)
            )
        await session.flush()
        return str(conversation.id)

    return run_db(seed)


class TestAuthentication:
    def test_every_endpoint_requires_a_session(self, client: TestClient) -> None:
        """§9: customer conversations are not public."""
        assert client.get("/api/v1/leads").status_code == 401
        assert client.get(f"/api/v1/leads/{uuid.uuid4()}").status_code == 401
        assert client.get("/api/v1/conversations").status_code == 401
        assert client.get(f"/api/v1/conversations/{uuid.uuid4()}").status_code == 401
        assert client.patch(f"/api/v1/leads/{uuid.uuid4()}", json={}).status_code == 401

    def test_a_salesperson_may_read_leads_and_conversations(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        """§9 restricts broadcasting, not reading — a salesperson working a lead needs
        to see what was said."""
        login(client, email=SALES_EMAIL)

        assert client.get("/api/v1/leads").status_code == 200
        assert client.get("/api/v1/conversations").status_code == 200


class TestLeadListing:
    def test_leads_are_listed_with_a_total(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        login(client)
        seed_lead(phone="+919876500011", name="One")
        seed_lead(phone="+919876500012", name="Two")

        body = client.get("/api/v1/leads").json()

        assert body["total"] == 2
        assert len(body["items"]) == 2

    def test_leads_can_be_filtered_by_status(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        login(client)
        seed_lead(phone="+919876500013", status=LeadStatus.QUALIFIED)
        seed_lead(phone="+919876500014", status=LeadStatus.NEW)

        body = client.get("/api/v1/leads?status=QUALIFIED").json()

        assert body["total"] == 1
        assert body["items"][0]["status"] == LeadStatus.QUALIFIED.value

    @pytest.mark.parametrize("term", ["Asha", "9876500015", "Northwind"])
    def test_search_matches_name_phone_and_company(
        self, client: TestClient, seeded_users: dict[str, str], term: str
    ) -> None:
        """Phone is searchable because it is the identity a salesperson actually has
        in front of them ([D11])."""
        login(client)
        seed_lead(phone="+919876500015", name="Asha Rao", company="Northwind")
        seed_lead(phone="+919876500016", name="Other Person", company="Elsewhere")

        body = client.get(f"/api/v1/leads?search={term}").json()

        assert body["total"] == 1
        assert body["items"][0]["phone_e164"] == "+919876500015"

    def test_paging_is_applied(self, client: TestClient, seeded_users: dict[str, str]) -> None:
        login(client)
        for index in range(5):
            seed_lead(phone=f"+91987650002{index}", name=f"Lead {index}")

        body = client.get("/api/v1/leads?limit=2&offset=2").json()

        assert body["total"] == 5
        assert len(body["items"]) == 2

    def test_a_missing_lead_is_404(self, client: TestClient, seeded_users: dict[str, str]) -> None:
        login(client)
        assert client.get(f"/api/v1/leads/{uuid.uuid4()}").status_code == 404

    def test_extracted_facts_are_visible(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        """§17 wants the AI's extraction visible to a human ([D18])."""
        login(client)
        lead_id = seed_lead(phone="+919876500030")

        body = client.get(f"/api/v1/leads/{lead_id}").json()

        assert body["qualification_data"] == {}


class TestLeadUpdate:
    def test_details_can_be_corrected(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        login(client)
        lead_id = seed_lead(phone="+919876500031", name="Typo")

        response = client.patch(
            f"/api/v1/leads/{lead_id}", json={"full_name": "Asha Rao", "city": "Pune"}
        )

        assert response.status_code == 200
        assert response.json()["full_name"] == "Asha Rao"
        assert response.json()["city"] == "Pune"

    def test_an_allowed_status_change_is_accepted(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        login(client)
        lead_id = seed_lead(phone="+919876500032", status=LeadStatus.RESPONDED)

        response = client.patch(f"/api/v1/leads/{lead_id}", json={"status": "INTERESTED"})

        assert response.status_code == 200
        assert response.json()["status"] == "INTERESTED"

    def test_a_disallowed_status_change_is_refused(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        """A human may correct a lead, but not reach a state §10 does not allow — the
        same table the worker obeys."""
        login(client)
        lead_id = seed_lead(phone="+919876500033", status=LeadStatus.NEW)

        response = client.patch(f"/api/v1/leads/{lead_id}", json={"status": "QUALIFIED"})

        assert response.status_code == 409

    def test_opting_a_lead_back_in_is_refused(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        """Consent is not an editable attribute: OPTED_OUT is terminal, so no PATCH can
        undo it."""
        login(client)
        lead_id = seed_lead(phone="+919876500034", status=LeadStatus.OPTED_OUT)

        assert (
            client.patch(f"/api/v1/leads/{lead_id}", json={"status": "RESPONDED"}).status_code
            == 409
        )

    def test_qualification_data_cannot_be_written(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        """[D18] makes it the AI's extraction target. An unknown field is ignored
        rather than silently applied."""
        login(client)
        lead_id = seed_lead(phone="+919876500035")

        response = client.patch(
            f"/api/v1/leads/{lead_id}", json={"qualification_data": {"budget": "large"}}
        )

        assert response.status_code == 200
        assert response.json()["qualification_data"] == {}

    def test_opted_out_cannot_be_written(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        login(client)
        lead_id = seed_lead(phone="+919876500036", status=LeadStatus.RESPONDED)

        response = client.patch(f"/api/v1/leads/{lead_id}", json={"opted_out": True})

        assert response.status_code == 200
        assert response.json()["opted_out"] is False

    def test_an_empty_patch_changes_nothing(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        login(client)
        lead_id = seed_lead(phone="+919876500037", name="Asha Rao")

        response = client.patch(f"/api/v1/leads/{lead_id}", json={})

        assert response.status_code == 200
        assert response.json()["full_name"] == "Asha Rao"

    def test_patching_a_missing_lead_is_404(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        login(client)
        assert client.patch(f"/api/v1/leads/{uuid.uuid4()}", json={}).status_code == 404


class TestConversationReading:
    def test_a_conversation_returns_its_transcript(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        login(client)
        lead_id = seed_lead(phone="+919876500040")
        conversation_id = seed_conversation(lead_id)

        body = client.get(f"/api/v1/conversations/{conversation_id}").json()

        assert body["total_turns"] == 2
        assert [turn["role"] for turn in body["turns"]] == ["user", "assistant"]
        assert body["lead"]["id"] == lead_id

    def test_the_transcript_is_chronological(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        login(client)
        lead_id = seed_lead(phone="+919876500041")
        conversation_id = seed_conversation(
            lead_id,
            turns=(("user", "first"), ("assistant", "second"), ("user", "third")),
        )

        body = client.get(f"/api/v1/conversations/{conversation_id}").json()

        assert [turn["content"] for turn in body["turns"]] == ["first", "second", "third"]

    def test_a_recent_conversation_is_inside_the_window(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        """§8: the dashboard needs this to explain whether a free-form reply is even
        possible."""
        login(client)
        lead_id = seed_lead(phone="+919876500042")
        conversation_id = seed_conversation(lead_id, last_inbound_age=timedelta(hours=1))

        body = client.get(f"/api/v1/conversations/{conversation_id}").json()

        assert body["within_service_window"] is True

    def test_a_stale_conversation_is_outside_the_window(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        login(client)
        lead_id = seed_lead(phone="+919876500043")
        conversation_id = seed_conversation(lead_id, last_inbound_age=timedelta(hours=30))

        body = client.get(f"/api/v1/conversations/{conversation_id}").json()

        assert body["within_service_window"] is False

    def test_a_conversation_with_no_inbound_message_is_outside_the_window(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        """No window has ever opened, so a free-form reply was never permitted."""
        login(client)
        lead_id = seed_lead(phone="+919876500044")
        conversation_id = seed_conversation(lead_id, last_inbound_age=None)

        body = client.get(f"/api/v1/conversations/{conversation_id}").json()

        assert body["within_service_window"] is False

    def test_a_missing_conversation_is_404(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        login(client)
        assert client.get(f"/api/v1/conversations/{uuid.uuid4()}").status_code == 404

    def test_conversations_can_be_filtered_by_status(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        login(client)
        first = seed_lead(phone="+919876500045")
        second = seed_lead(phone="+919876500046")
        seed_conversation(first, status=ConversationStatus.ACTIVE)
        seed_conversation(second, status=ConversationStatus.CLOSED)

        body = client.get("/api/v1/conversations?status=CLOSED").json()

        assert body["total"] == 1
        assert body["items"][0]["status"] == "CLOSED"


class TestLeadConversation:
    def test_a_lead_returns_its_conversation(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        login(client)
        lead_id = seed_lead(phone="+919876500047")
        conversation_id = seed_conversation(lead_id)

        body = client.get(f"/api/v1/leads/{lead_id}/conversation").json()

        assert body["conversation"]["id"] == conversation_id

    def test_the_most_recent_conversation_is_returned(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        """[D36] starts a new conversation after a closed one, so a lead can have
        several. The current one is the one worth showing."""
        login(client)
        lead_id = seed_lead(phone="+919876500048")
        seed_conversation(lead_id, status=ConversationStatus.CLOSED, turns=(("user", "old"),))
        newest = seed_conversation(
            lead_id, status=ConversationStatus.ACTIVE, turns=(("user", "new"),)
        )

        body = client.get(f"/api/v1/leads/{lead_id}/conversation").json()

        assert body["conversation"]["id"] == newest

    def test_a_closed_conversation_is_still_readable(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        """Ending a conversation must not hide what was said in it."""
        login(client)
        lead_id = seed_lead(phone="+919876500049")
        seed_conversation(lead_id, status=ConversationStatus.CLOSED)

        body = client.get(f"/api/v1/leads/{lead_id}/conversation").json()

        assert body["conversation"]["status"] == "CLOSED"
        assert body["total_turns"] == 2

    def test_a_lead_with_no_conversation_is_404(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        login(client)
        lead_id = seed_lead(phone="+919876500050")

        assert client.get(f"/api/v1/leads/{lead_id}/conversation").status_code == 404


class TestNoManualSending:
    def test_there_is_no_endpoint_to_send_a_message(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        """Deliberate: the worker is where the window and suppression checks live, and
        an endpoint that bypassed them would be a way to message someone who opted
        out."""
        login(client)
        lead_id = seed_lead(phone="+919876500051")
        conversation_id = seed_conversation(lead_id)

        for path in (
            f"/api/v1/conversations/{conversation_id}/messages",
            f"/api/v1/conversations/{conversation_id}/reply",
            f"/api/v1/leads/{lead_id}/messages",
        ):
            assert client.post(path, json={"body": "hello"}).status_code == 404


class TestTranscriptOrdering:
    """Turns written in one transaction share a `created_at`.

    `created_at` defaults to `now()`, which in PostgreSQL is the transaction timestamp,
    so ordering by it leaves the transcript order to the planner — and that order is
    what the AI reads the conversation in. These pin the sequence-based ordering that
    replaced it.
    """

    def test_turns_written_together_keep_their_order(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        login(client)
        lead_id = seed_lead(phone="+919876500060")
        expected = [f"turn {index}" for index in range(12)]
        conversation_id = seed_conversation(
            lead_id, turns=tuple(("user", content) for content in expected)
        )

        body = client.get(f"/api/v1/conversations/{conversation_id}").json()

        assert [turn["content"] for turn in body["turns"]] == expected

    def test_they_do_share_a_created_at(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        """The condition the ordering fix exists for — asserted so nobody later
        'simplifies' the sequence column away believing timestamps are enough."""
        lead_id = seed_lead(phone="+919876500061")
        conversation_id = seed_conversation(
            lead_id, turns=(("user", "a"), ("assistant", "b"), ("user", "c"))
        )

        async def timestamps(session: AsyncSession) -> set[Any]:
            from sqlalchemy import select

            result = await session.execute(
                select(ConversationMessage.created_at).where(
                    ConversationMessage.conversation_id == uuid.UUID(conversation_id)
                )
            )
            return set(result.scalars().all())

        assert len(run_db(timestamps)) == 1

    def test_the_most_recent_turns_are_kept_when_limited(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        """A finite context window means keeping the end of the conversation, not its
        beginning — but still presented oldest-first."""
        login(client)
        lead_id = seed_lead(phone="+919876500062")
        conversation_id = seed_conversation(
            lead_id, turns=tuple(("user", f"turn {index}") for index in range(10))
        )

        body = client.get(f"/api/v1/conversations/{conversation_id}?limit=3").json()

        assert [turn["content"] for turn in body["turns"]] == ["turn 7", "turn 8", "turn 9"]
        # The count reports the whole transcript, not the page.
        assert body["total_turns"] == 10
