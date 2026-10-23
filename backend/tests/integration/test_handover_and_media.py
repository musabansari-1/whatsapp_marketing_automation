# ruff: noqa: F811 - the turn and booking fixtures are imported, then named as parameters
"""Team handover, media replies, the hot-lead bell and the one-round reply.

Against real PostgreSQL, with both providers faked: no message leaves and no model
is called.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.domain.enums import LeadStatus, NotificationType
from app.integrations.llm import FakeLLMProvider, LLMCompletion, ToolCall
from app.integrations.queue import EnqueuedTask, TaskRequest, TaskType
from app.integrations.whatsapp import FakeWhatsAppProvider
from app.persistence.models import Conversation, Message
from app.worker.handlers.conversation import MEDIA_TURN_PLACEHOLDER, reply_idempotency_key
from tests.integration.helpers import run_db
from tests.integration.test_booking_flow import booking_env, notifications, seed  # noqa: F401
from tests.integration.test_conversation_turn import (
    outbound_messages,
    run_turn,
    seed_turn,
    transcript,
    turn_env,  # noqa: F401
)

pytestmark = pytest.mark.integration

Env = tuple[FakeLLMProvider, FakeWhatsAppProvider]


def pause(conversation_id: str, until: datetime) -> None:
    async def write(session: AsyncSession) -> None:
        await session.execute(
            update(Conversation)
            .where(Conversation.id == uuid.UUID(conversation_id))
            .values(ai_paused_until=until)
        )

    run_db(write)


def clear_body(message_id: str) -> None:
    async def write(session: AsyncSession) -> None:
        await session.execute(
            update(Message).where(Message.id == uuid.UUID(message_id)).values(body=None)
        )

    run_db(write)


def recording_queue(monkeypatch: pytest.MonkeyPatch) -> list[TaskRequest]:
    enqueued: list[TaskRequest] = []

    class RecordingQueue:
        async def enqueue(self, task: TaskRequest) -> EnqueuedTask:
            enqueued.append(task)
            return EnqueuedTask(task_id=task.task_id, task_type=task.task_type)

    monkeypatch.setattr(
        "app.worker.handlers.conversation.build_queue_provider", lambda settings: RecordingQueue()
    )
    return enqueued


class TestAssistantPause:
    def test_a_paused_conversation_records_but_does_not_answer(self, turn_env: Env) -> None:
        llm, whatsapp = turn_env
        _lead, conversation_id, message_id = seed_turn(body="Is anyone there?")
        pause(conversation_id, datetime.now(UTC) + timedelta(hours=2))

        result = run_turn(message_id)

        assert result["result"] == "ai_paused"
        assert whatsapp.sent_texts == []
        assert llm.call_count == 0
        assert transcript(conversation_id) == [("user", "Is anyone there?")]

    def test_an_expired_pause_answers_again(self, turn_env: Env) -> None:
        _llm, whatsapp = turn_env
        _lead, conversation_id, message_id = seed_turn()
        pause(conversation_id, datetime.now(UTC) - timedelta(minutes=1))

        assert run_turn(message_id)["result"] == "replied"
        assert len(whatsapp.sent_texts) == 1


class TestMediaReply:
    def test_a_voice_note_gets_the_fixed_reply_without_a_model_call(self, turn_env: Env) -> None:
        llm, whatsapp = turn_env
        _lead, conversation_id, message_id = seed_turn()
        clear_body(message_id)

        result = run_turn(message_id)

        assert result["result"] == "media_reply"
        assert llm.call_count == 0
        assert [m.body for m in whatsapp.sent_texts] == [get_settings().media_reply_message]
        # Under the turn's own key, so the redelivery check and the sweep see it answered.
        assert outbound_messages(conversation_id)[0].idempotency_key == reply_idempotency_key(
            message_id
        )
        assert transcript(conversation_id)[0] == ("user", MEDIA_TURN_PLACEHOLDER)

    def test_a_burst_of_photos_gets_one_reply(self, turn_env: Env) -> None:
        _llm, whatsapp = turn_env
        _lead, _conversation_id, first = seed_turn()
        clear_body(first)
        run_turn(first)

        async def second_photo(session: AsyncSession) -> str:
            original = await session.get(Message, uuid.UUID(first))
            assert original is not None
            row = Message(
                lead_id=original.lead_id,
                conversation_id=original.conversation_id,
                product_id=original.product_id,
                direction=original.direction,
                status=original.status,
                provider_message_id=f"wamid-{uuid.uuid4()}",
                phone_e164=original.phone_e164,
                body=None,
            )
            session.add(row)
            await session.flush()
            return str(row.id)

        result = run_turn(run_db(second_photo))

        assert result["result"] == "media_reply_cooldown"
        assert len(whatsapp.sent_texts) == 1


class TestHotLeadAndOneRound:
    def test_a_reply_written_with_the_facts_is_sent_after_one_model_call(
        self, turn_env: Env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        llm, whatsapp = turn_env
        llm.queue_completion(
            LLMCompletion(
                text="Great! Here is the doctor signup link.",
                model=llm.model,
                tool_calls=(
                    ToolCall(
                        id="c1",
                        name="update_lead",
                        arguments={"facts": {"interested": True, "registration_intent": True}},
                    ),
                ),
                finish_reason="tool_calls",
            )
        )
        enqueued = recording_queue(monkeypatch)
        lead_id, _conversation_id, message_id = seed_turn(lead_status=LeadStatus.RESPONDED)

        assert run_turn(message_id)["result"] == "replied"

        assert llm.call_count == 1
        assert [m.body for m in whatsapp.sent_texts] == ["Great! Here is the doctor signup link."]
        bells = [t for t in enqueued if t.task_type is TaskType.NOTIFY_LEAD_ATTENTION]
        assert len(bells) == 1
        assert bells[0].payload == {"lead_id": lead_id, "reason": "wants to register"}

    def test_an_ordinary_reply_rings_no_bell(
        self, turn_env: Env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        llm, _whatsapp = turn_env
        llm.queue_text("Hello! How can I help?")
        enqueued = recording_queue(monkeypatch)
        _lead, _conversation_id, message_id = seed_turn()

        run_turn(message_id)

        assert [t for t in enqueued if t.task_type is TaskType.NOTIFY_LEAD_ATTENTION] == []


def run_attention(lead_id: str, reason: str) -> dict[str, Any]:
    from app.persistence import database
    from app.worker.handlers.lead_attention import notify_lead_attention

    async def run() -> dict[str, Any]:
        database._engine = None  # noqa: SLF001
        database._session_factory = None  # noqa: SLF001
        try:
            return dict(
                await notify_lead_attention(
                    TaskRequest(
                        task_type=TaskType.NOTIFY_LEAD_ATTENTION,
                        task_id=f"notify-lead-attention-{lead_id}",
                        payload={"lead_id": lead_id, "reason": reason},
                    )
                )
            )
        finally:
            await database.dispose_engine()

    return asyncio.run(run())


class TestLeadAttentionNotification:
    def test_every_active_user_hears_once(self, booking_env: FakeWhatsAppProvider) -> None:
        lead_id, _ = seed(users=2)

        first = run_attention(lead_id, "wants to register")
        second = run_attention(lead_id, "asked to speak to a person")

        assert first["notified"] == 2
        assert second["notified"] == 0
        rows = notifications()
        assert {row.type for row in rows} == {NotificationType.LEAD_NEEDS_ATTENTION}
        assert "Ready to register" in rows[0].title
        assert "wants to register" in rows[0].message


def run_team_reply(conversation_id: str, body: str = "Hi, this is Riya from the team.") -> str:
    from app.persistence import database
    from app.worker.handlers.team_reply import send_team_reply

    async def run() -> str:
        database._engine = None  # noqa: SLF001
        database._session_factory = None  # noqa: SLF001
        try:
            result = await send_team_reply(
                TaskRequest(
                    task_type=TaskType.SEND_TEAM_REPLY,
                    task_id="team-reply-1",
                    payload={
                        "conversation_id": conversation_id,
                        "request_id": "req-1",
                        "body": body,
                        "user_id": None,
                    },
                )
            )
            return str(result["result"])
        finally:
            await database.dispose_engine()

    return asyncio.run(run())


class TestTeamReply:
    @pytest.fixture(autouse=True)
    def _team_reply_sends_through_the_fake(
        self, turn_env: Env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _llm, whatsapp = turn_env
        monkeypatch.setattr(
            "app.worker.handlers.team_reply.build_whatsapp_provider",
            lambda _settings, **_: whatsapp,
        )

    def test_the_reply_is_sent_and_marked_as_the_teams(self, turn_env: Env) -> None:
        _llm, whatsapp = turn_env
        _lead, conversation_id, _message = seed_turn()

        assert run_team_reply(conversation_id) == "sent"
        assert run_team_reply(conversation_id) == "already_sent"

        assert [m.body for m in whatsapp.sent_texts] == ["Hi, this is Riya from the team."]
        assert transcript(conversation_id)[-1] == ("assistant", "Hi, this is Riya from the team.")

    def test_a_suppressed_number_is_not_messaged(self, turn_env: Env) -> None:
        _llm, whatsapp = turn_env
        _lead, conversation_id, _message = seed_turn(suppressed=True)

        assert run_team_reply(conversation_id) == "suppressed"
        assert whatsapp.sent_texts == []

    def test_outside_the_window_nothing_is_sent(self, turn_env: Env) -> None:
        _llm, whatsapp = turn_env
        _lead, conversation_id, _message = seed_turn(last_inbound_age=timedelta(hours=30))

        assert run_team_reply(conversation_id) == "outside_service_window"
        assert whatsapp.sent_texts == []
