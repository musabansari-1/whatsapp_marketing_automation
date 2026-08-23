"""Conversation turn handler tests (§9, §10, §18, §20, [D16], [D36]).

The whole inbound flow against real PostgreSQL: history, states, the 24-hour window,
suppression, idempotency and reply delivery.

Nothing here reaches a network. The LLM is `FakeLLMProvider` and delivery is
`FakeWhatsAppProvider`, so no message can leave and no model is called.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NonRetryableProviderError, RetryableProviderError
from app.domain.enums import (
    ConversationStatus,
    LeadStatus,
    MessageDirection,
    MessageStatus,
)
from app.integrations.llm import FakeLLMProvider
from app.integrations.queue import TaskRequest, TaskType
from app.integrations.whatsapp import FakeWhatsAppProvider
from app.persistence.models import (
    AIRun,
    Conversation,
    ConversationMessage,
    Lead,
    Message,
    WhatsAppSuppression,
)
from app.persistence.repositories import conversations as conversation_repo
from app.worker.handlers.campaign_send import RetryableSendError
from app.worker.handlers.conversation import process_conversation_turn
from tests.integration.helpers import get_test_database_url, run_db

pytestmark = pytest.mark.integration

PHONE = "+919876500001"


@pytest.fixture
def turn_env(
    engine: Any, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[FakeLLMProvider, FakeWhatsAppProvider]]:
    """A clean conversation schema with both providers faked and inspectable."""
    from app.core.config import reset_settings_cache
    from app.persistence import database

    monkeypatch.setenv("DATABASE_URL", get_test_database_url())
    reset_settings_cache()
    database._engine = None  # noqa: SLF001
    database._session_factory = None  # noqa: SLF001

    llm = FakeLLMProvider(default_text="Thanks for your message. How can I help?")
    whatsapp = FakeWhatsAppProvider()
    monkeypatch.setattr(
        "app.worker.handlers.conversation.build_llm_provider", lambda _settings: llm
    )
    monkeypatch.setattr(
        "app.worker.handlers.conversation.build_whatsapp_provider", lambda _settings: whatsapp
    )

    async def clear(session: AsyncSession) -> None:
        await session.execute(delete(AIRun))
        await session.execute(delete(ConversationMessage))
        await session.execute(delete(Message))
        await session.execute(delete(Conversation))
        await session.execute(delete(WhatsAppSuppression))
        await session.execute(delete(Lead))

    run_db(clear)
    yield llm, whatsapp
    run_db(clear)


def seed_turn(
    *,
    conversation_status: ConversationStatus = ConversationStatus.ACTIVE,
    lead_status: LeadStatus = LeadStatus.CONTACTED,
    last_inbound_age: timedelta = timedelta(minutes=1),
    body: str = "Do you offer training as well?",
    suppressed: bool = False,
) -> tuple[str, str, str]:
    """One lead, one conversation and one stored inbound message.

    Returns the lead, conversation and message IDs.
    """

    async def seed(session: AsyncSession) -> tuple[str, str, str]:
        received_at = datetime.now(UTC) - last_inbound_age

        lead = Lead(phone_e164=PHONE, full_name="Asha", status=lead_status)
        session.add(lead)
        await session.flush()

        conversation = Conversation(
            lead_id=lead.id,
            status=conversation_status,
            last_inbound_at=received_at,
        )
        session.add(conversation)
        await session.flush()

        message = Message(
            lead_id=lead.id,
            conversation_id=conversation.id,
            direction=MessageDirection.INBOUND,
            status=MessageStatus.DELIVERED,
            provider_message_id=f"wamid-{uuid.uuid4()}",
            phone_e164=PHONE,
            body=body,
            delivered_at=received_at,
        )
        session.add(message)

        if suppressed:
            session.add(WhatsAppSuppression(phone_e164=PHONE, lead_id=lead.id))

        await session.flush()
        return str(lead.id), str(conversation.id), str(message.id)

    return run_db(seed)


def run_turn(message_id: str) -> dict[str, Any]:
    """Run the handler on its own event loop, as the worker would."""
    from app.persistence import database

    async def run() -> dict[str, Any]:
        database._engine = None  # noqa: SLF001
        database._session_factory = None  # noqa: SLF001
        try:
            result = await process_conversation_turn(
                TaskRequest(
                    task_type=TaskType.PROCESS_CONVERSATION_TURN,
                    task_id=f"turn-{message_id}",
                    payload={"message_id": message_id},
                )
            )
            return dict(result)
        finally:
            await database.dispose_engine()

    return asyncio.run(run())


def transcript(conversation_id: str) -> list[tuple[str, str]]:
    async def fetch(session: AsyncSession) -> list[tuple[str, str]]:
        turns = await conversation_repo.get_history(session, uuid.UUID(conversation_id))
        return [(turn.role, turn.content) for turn in turns]

    return run_db(fetch)


def conversation_row(conversation_id: str) -> Conversation:
    async def fetch(session: AsyncSession) -> Conversation:
        row = await conversation_repo.get_conversation(session, uuid.UUID(conversation_id))
        assert row is not None
        return row

    return run_db(fetch)


def lead_row(lead_id: str) -> Lead:
    async def fetch(session: AsyncSession) -> Lead:
        row = await conversation_repo.get_lead(session, uuid.UUID(lead_id))
        assert row is not None
        return row

    return run_db(fetch)


def outbound_messages(conversation_id: str) -> list[Message]:
    async def fetch(session: AsyncSession) -> list[Message]:
        result = await session.execute(
            select(Message).where(
                Message.conversation_id == uuid.UUID(conversation_id),
                Message.direction == MessageDirection.OUTBOUND,
            )
        )
        return list(result.scalars().all())

    return run_db(fetch)


def ai_runs() -> list[AIRun]:
    async def fetch(session: AsyncSession) -> list[AIRun]:
        result = await session.execute(select(AIRun))
        return list(result.scalars().all())

    return run_db(fetch)


class TestHappyPath:
    def test_a_reply_is_generated_sent_and_recorded(
        self, turn_env: tuple[FakeLLMProvider, FakeWhatsAppProvider]
    ) -> None:
        llm, whatsapp = turn_env
        _lead_id, conversation_id, message_id = seed_turn()

        result = run_turn(message_id)

        assert result["result"] == "replied"
        assert result["replied"] is True
        assert len(whatsapp.sent_texts) == 1
        assert whatsapp.sent_texts[0].to_e164 == PHONE

        messages = outbound_messages(conversation_id)
        assert len(messages) == 1
        assert messages[0].status is MessageStatus.SENT
        assert messages[0].body == llm.default_text

    def test_both_turns_reach_the_transcript(
        self, turn_env: tuple[FakeLLMProvider, FakeWhatsAppProvider]
    ) -> None:
        llm, _whatsapp = turn_env
        _lead_id, conversation_id, message_id = seed_turn(body="What is the price?")

        run_turn(message_id)

        assert transcript(conversation_id) == [
            ("user", "What is the price?"),
            ("assistant", llm.default_text),
        ]

    def test_the_model_receives_the_history(
        self, turn_env: tuple[FakeLLMProvider, FakeWhatsAppProvider]
    ) -> None:
        """The prompt is the transcript, not just the latest message: a reply that
        ignores what was already said reads as though nobody was listening."""
        llm, _whatsapp = turn_env
        _lead_id, conversation_id, message_id = seed_turn(body="And what about support?")

        run_turn(message_id)

        sent_messages = llm.requests[0].messages
        assert sent_messages[0].role.value == "system"
        assert any("support" in message.content for message in sent_messages)

    def test_the_system_prompt_comes_from_configuration(
        self, turn_env: tuple[FakeLLMProvider, FakeWhatsAppProvider]
    ) -> None:
        """[D21]: customer-facing behaviour is configuration, never a literal."""
        from app.core.config import get_settings

        llm, _whatsapp = turn_env
        _lead_id, _conversation_id, message_id = seed_turn()

        run_turn(message_id)

        assert llm.requests[0].messages[0].content == get_settings().ai_system_prompt

    def test_the_conversation_waits_for_the_customer(
        self, turn_env: tuple[FakeLLMProvider, FakeWhatsAppProvider]
    ) -> None:
        _lead_id, conversation_id, message_id = seed_turn()

        run_turn(message_id)

        row = conversation_row(conversation_id)
        assert row.status is ConversationStatus.WAITING_FOR_USER
        assert row.last_outbound_at is not None

    def test_a_booking_conversation_stays_in_booking(
        self, turn_env: tuple[FakeLLMProvider, FakeWhatsAppProvider]
    ) -> None:
        """§10 has no edge treating a wait for a slot choice as ordinary waiting, and
        the handler must not force one."""
        _lead_id, conversation_id, message_id = seed_turn(
            conversation_status=ConversationStatus.BOOKING
        )

        run_turn(message_id)

        assert conversation_row(conversation_id).status is ConversationStatus.BOOKING

    def test_a_contacted_lead_becomes_responded(
        self, turn_env: tuple[FakeLLMProvider, FakeWhatsAppProvider]
    ) -> None:
        lead_id, _conversation_id, message_id = seed_turn(lead_status=LeadStatus.CONTACTED)

        run_turn(message_id)

        assert lead_row(lead_id).status is LeadStatus.RESPONDED

    def test_a_further_along_lead_is_not_walked_backwards(
        self, turn_env: tuple[FakeLLMProvider, FakeWhatsAppProvider]
    ) -> None:
        """A QUALIFYING lead has obviously responded; §10 permits no edge back."""
        lead_id, _conversation_id, message_id = seed_turn(lead_status=LeadStatus.QUALIFYING)

        run_turn(message_id)

        assert lead_row(lead_id).status is LeadStatus.QUALIFYING

    def test_the_run_is_recorded_for_observability(
        self, turn_env: tuple[FakeLLMProvider, FakeWhatsAppProvider]
    ) -> None:
        """§21 wants AI invocations visible, and [D17] means the model may change
        between runs — so it is recorded per run rather than assumed."""
        llm, _whatsapp = turn_env
        _lead_id, _conversation_id, message_id = seed_turn()

        run_turn(message_id)

        runs = ai_runs()
        assert len(runs) == 1
        assert runs[0].model == llm.model
        assert runs[0].latency_ms is not None


class TestIdempotency:
    def test_a_redelivered_task_does_not_reply_twice(
        self, turn_env: tuple[FakeLLMProvider, FakeWhatsAppProvider]
    ) -> None:
        """§20: Cloud Tasks redelivers. A second reply would be a duplicate message to
        a real customer."""
        _llm, whatsapp = turn_env
        _lead_id, conversation_id, message_id = seed_turn()

        first = run_turn(message_id)
        second = run_turn(message_id)

        assert first["result"] == "replied"
        assert second["result"] == "already_replied"
        assert len(whatsapp.sent_texts) == 1
        assert len(outbound_messages(conversation_id)) == 1

    def test_the_transcript_is_not_duplicated(
        self, turn_env: tuple[FakeLLMProvider, FakeWhatsAppProvider]
    ) -> None:
        _lead_id, conversation_id, message_id = seed_turn()

        run_turn(message_id)
        run_turn(message_id)

        assert len(transcript(conversation_id)) == 2

    def test_the_model_is_not_called_again(
        self, turn_env: tuple[FakeLLMProvider, FakeWhatsAppProvider]
    ) -> None:
        """A redelivery must not cost a second completion."""
        llm, _whatsapp = turn_env
        _lead_id, _conversation_id, message_id = seed_turn()

        run_turn(message_id)
        run_turn(message_id)

        assert len(llm.requests) == 1

    def test_the_idempotency_key_is_derived_from_the_message(
        self, turn_env: tuple[FakeLLMProvider, FakeWhatsAppProvider]
    ) -> None:
        """Derived rather than generated, so a retry computes the same key and the
        unique constraint refuses the second write."""
        _lead_id, conversation_id, message_id = seed_turn()

        run_turn(message_id)

        assert outbound_messages(conversation_id)[0].idempotency_key == f"reply-{message_id}"


class TestRefusals:
    def test_a_closed_conversation_gets_no_reply(
        self, turn_env: tuple[FakeLLMProvider, FakeWhatsAppProvider]
    ) -> None:
        """[D36]: closed is terminal. Replying would reopen it by the back door."""
        _llm, whatsapp = turn_env
        _lead_id, conversation_id, message_id = seed_turn(
            conversation_status=ConversationStatus.CLOSED
        )

        result = run_turn(message_id)

        assert result["result"] == "conversation_closed"
        assert whatsapp.sent_texts == []
        assert conversation_row(conversation_id).status is ConversationStatus.CLOSED

    def test_a_suppressed_number_gets_no_reply(
        self, turn_env: tuple[FakeLLMProvider, FakeWhatsAppProvider]
    ) -> None:
        """[D16]: consent may have been withdrawn after the task was queued, and the
        suppression table is the authoritative check."""
        _llm, whatsapp = turn_env
        _lead_id, _conversation_id, message_id = seed_turn(suppressed=True)

        result = run_turn(message_id)

        assert result["result"] == "suppressed"
        assert whatsapp.sent_texts == []

    def test_a_reply_outside_the_service_window_is_not_sent(
        self, turn_env: tuple[FakeLLMProvider, FakeWhatsAppProvider]
    ) -> None:
        """§8: past 24 hours Meta permits only an approved template, and [D24] leaves
        that content with the client."""
        _llm, whatsapp = turn_env
        _lead_id, _conversation_id, message_id = seed_turn(last_inbound_age=timedelta(hours=30))

        result = run_turn(message_id)

        assert result["result"] == "outside_service_window"
        assert whatsapp.sent_texts == []

    def test_the_missed_turn_is_still_recorded(
        self, turn_env: tuple[FakeLLMProvider, FakeWhatsAppProvider]
    ) -> None:
        """The transcript stays complete, so the dashboard can show what was missed
        rather than silently losing the customer's message."""
        _lead_id, conversation_id, message_id = seed_turn(
            last_inbound_age=timedelta(hours=30), body="Are you still there?"
        )

        run_turn(message_id)

        assert transcript(conversation_id) == [("user", "Are you still there?")]

    def test_an_empty_completion_falls_back_rather_than_going_silent(
        self, turn_env: tuple[FakeLLMProvider, FakeWhatsAppProvider]
    ) -> None:
        """Phase 10 changed this deliberately.

        Phase 9 sent nothing when the model returned nothing, which leaves the customer
        on read. §11 says insufficient output must become the approved fallback ([D21])
        rather than silence — an empty WhatsApp message is not an option either.
        """
        from app.core.config import get_settings

        llm, whatsapp = turn_env
        llm.default_text = "   "
        _lead_id, _conversation_id, message_id = seed_turn()

        result = run_turn(message_id)

        assert result["result"] == "replied"
        assert len(whatsapp.sent_texts) == 1
        assert whatsapp.sent_texts[0].body == get_settings().msg_rag_fallback

    def test_a_message_outside_a_conversation_is_rejected(
        self, turn_env: tuple[FakeLLMProvider, FakeWhatsAppProvider]
    ) -> None:
        with pytest.raises(ValueError, match="message_id"):
            asyncio.run(
                process_conversation_turn(
                    TaskRequest(
                        task_type=TaskType.PROCESS_CONVERSATION_TURN,
                        task_id="turn-none",
                        payload={},
                    )
                )
            )


class TestFailures:
    def test_a_transient_llm_failure_is_retried(
        self, turn_env: tuple[FakeLLMProvider, FakeWhatsAppProvider]
    ) -> None:
        """Raising is what makes the worker answer 503 with Retry-After — the retry
        protocol lives in the HTTP status."""
        llm, whatsapp = turn_env
        llm.fail_with = RetryableProviderError("upstream unavailable", provider="fake")
        _lead_id, _conversation_id, message_id = seed_turn()

        with pytest.raises(RetryableSendError):
            run_turn(message_id)

        assert whatsapp.sent_texts == []

    def test_a_permanent_llm_failure_settles(
        self, turn_env: tuple[FakeLLMProvider, FakeWhatsAppProvider]
    ) -> None:
        """Retrying a rejected request cannot help; returning lets the task finish."""
        llm, whatsapp = turn_env
        llm.fail_with = NonRetryableProviderError("model refused the request", provider="fake")
        _lead_id, _conversation_id, message_id = seed_turn()

        result = run_turn(message_id)

        assert result["result"] == "llm_failed"
        assert whatsapp.sent_texts == []

    def test_a_failed_run_is_still_recorded(
        self, turn_env: tuple[FakeLLMProvider, FakeWhatsAppProvider]
    ) -> None:
        """§21: a failure that leaves no trail is the one nobody can diagnose."""
        llm, _whatsapp = turn_env
        llm.fail_with = NonRetryableProviderError("model refused", provider="fake")
        _lead_id, _conversation_id, message_id = seed_turn()

        run_turn(message_id)

        runs = ai_runs()
        assert len(runs) == 1
        assert runs[0].error_message

    def test_a_transient_send_failure_is_retried(
        self, turn_env: tuple[FakeLLMProvider, FakeWhatsAppProvider]
    ) -> None:
        _llm, whatsapp = turn_env
        whatsapp.fail_with = RetryableProviderError("rate limit", provider="fake")
        _lead_id, _conversation_id, message_id = seed_turn()

        with pytest.raises(RetryableSendError):
            run_turn(message_id)

    def test_a_retry_after_a_send_failure_succeeds(
        self, turn_env: tuple[FakeLLMProvider, FakeWhatsAppProvider]
    ) -> None:
        """The reply was never delivered, so the retry must be free to send it."""
        _llm, whatsapp = turn_env
        whatsapp.fail_with = RetryableProviderError("rate limit", provider="fake")
        _lead_id, conversation_id, message_id = seed_turn()

        with pytest.raises(RetryableSendError):
            run_turn(message_id)

        whatsapp.fail_with = None
        assert run_turn(message_id)["result"] == "replied"
        assert len(outbound_messages(conversation_id)) == 1


class TestToolCalls:
    def test_an_authorized_tool_runs_and_is_recorded(
        self, turn_env: tuple[FakeLLMProvider, FakeWhatsAppProvider]
    ) -> None:
        """Phase 10 executes the authorized six; Phase 9 recorded and ignored them.

        The trail records what ran *and* its result, which is what §21 needs to explain
        why a customer was told what they were told.
        """
        llm, _whatsapp = turn_env
        llm.queue_tool_call("get_lead", {})
        llm.queue_text("You are with Northwind, is that right?")
        _lead_id, _conversation_id, message_id = seed_turn()

        result = run_turn(message_id)

        assert result["result"] == "replied"
        runs = ai_runs()
        recorded = runs[0].tool_calls
        assert recorded is not None
        executed = recorded["executed"]
        assert executed[0]["name"] == "get_lead"
        assert executed[0]["ok"] is True

    def test_an_unauthorized_tool_is_refused(
        self, turn_env: tuple[FakeLLMProvider, FakeWhatsAppProvider]
    ) -> None:
        """§9 lists six tools. A hallucinated name is an error, never an action — a
        fuzzy match would run something the conversation never called for."""
        llm, _whatsapp = turn_env
        llm.queue_tool_call("delete_all_leads", {})
        llm.queue_text("Let me check on that for you.")
        _lead_id, _conversation_id, message_id = seed_turn()

        run_turn(message_id)

        recorded = ai_runs()[0].tool_calls
        assert recorded is not None
        executed = recorded["executed"]
        assert executed[0]["name"] == "delete_all_leads"
        assert executed[0]["ok"] is False
        assert "not an available tool" in executed[0]["error"]

    def test_update_lead_records_facts_without_setting_status(
        self, turn_env: tuple[FakeLLMProvider, FakeWhatsAppProvider]
    ) -> None:
        """[D18]: the model extracts, the backend decides. The tool has no `status`
        parameter, so this is structural rather than a matter of the model behaving."""
        llm, _whatsapp = turn_env
        llm.queue_tool_call(
            "update_lead",
            {"company": "Northwind", "status": "QUALIFIED", "facts": {"team_size": 40}},
        )
        llm.queue_text("Thanks, noted.")
        lead_id, _conversation_id, message_id = seed_turn(lead_status=LeadStatus.RESPONDED)

        run_turn(message_id)

        lead = lead_row(lead_id)
        assert lead.company == "Northwind"
        assert lead.qualification_data["team_size"] == 40
        # Not QUALIFIED: the model asked, and the tool has no way to grant it.
        assert lead.status is not LeadStatus.QUALIFIED

    def test_the_tool_loop_is_bounded(
        self, turn_env: tuple[FakeLLMProvider, FakeWhatsAppProvider]
    ) -> None:
        """A model that keeps calling tools would otherwise spend the customer's time
        and the account's budget indefinitely."""
        llm, whatsapp = turn_env
        for _ in range(10):
            llm.queue_tool_call("get_lead", {})
        llm.queue_text("Sorry about that.")
        _lead_id, _conversation_id, message_id = seed_turn()

        run_turn(message_id)

        # The final round is asked without tools, so the loop terminates in words.
        assert len(llm.requests) <= 4
        assert len(whatsapp.sent_texts) == 1
