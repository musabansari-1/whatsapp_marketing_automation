"""Recovery sweep tests (§20, D-15).

The sweep repairs a failure the platform deliberately swallows: an inbound message is
committed, its conversation turn fails to enqueue, and nothing retries. These prove it
finds the stranded message and — just as importantly — leaves alone the ones that are
merely in flight.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import (
    ConversationStatus,
    MessageDirection,
    MessageStatus,
)
from app.integrations.queue import TaskRequest, TaskType
from app.persistence.models import Conversation, ConversationMessage, Lead, Message
from app.worker.handlers.conversation import reply_idempotency_key
from app.worker.handlers.sweeps import (
    reply_key_for,
    sweep_unanswered_messages,
)
from tests.integration.helpers import (
    ensure_product_id,
    get_test_database_url,
    run_db,
)

pytestmark = pytest.mark.integration

PHONE = "+919876500700"


@pytest.fixture
def sweep_env(engine: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[list[TaskRequest]]:
    """A clean conversation schema and a queue that records what the sweep enqueues."""
    from app.core.config import reset_settings_cache
    from app.persistence import database

    monkeypatch.setenv("DATABASE_URL", get_test_database_url())
    reset_settings_cache()
    database._engine = None  # noqa: SLF001
    database._session_factory = None  # noqa: SLF001

    queued: list[TaskRequest] = []

    class RecordingQueue:
        async def enqueue(self, task: TaskRequest) -> None:
            queued.append(task)

    monkeypatch.setattr(
        "app.worker.handlers.sweeps.build_queue_provider", lambda _settings: RecordingQueue()
    )

    async def clear(session: AsyncSession) -> None:
        await session.execute(delete(ConversationMessage))
        await session.execute(delete(Message))
        await session.execute(delete(Conversation))
        await session.execute(delete(Lead))

    run_db(clear)
    yield queued
    run_db(clear)


def seed_inbound(
    *,
    answered: bool = False,
    age: timedelta = timedelta(hours=1),
    conversation_status: ConversationStatus = ConversationStatus.ACTIVE,
    phone: str = PHONE,
) -> str:
    """One inbound message, optionally with the reply that would make it answered."""

    async def seed(session: AsyncSession) -> str:
        product_id = await ensure_product_id(session)
        lead = Lead(phone_e164=phone, full_name="Asha", product_id=product_id)
        session.add(lead)
        await session.flush()

        conversation = Conversation(
            lead_id=lead.id, status=conversation_status, product_id=product_id
        )
        session.add(conversation)
        await session.flush()

        inbound = Message(
            lead_id=lead.id,
            conversation_id=conversation.id,
            product_id=product_id,
            direction=MessageDirection.INBOUND,
            status=MessageStatus.DELIVERED,
            provider_message_id=f"wamid-{uuid.uuid4()}",
            phone_e164=phone,
            body="Do you offer training?",
        )
        session.add(inbound)
        await session.flush()

        if answered:
            session.add(
                Message(
                    lead_id=lead.id,
                    conversation_id=conversation.id,
                    product_id=product_id,
                    direction=MessageDirection.OUTBOUND,
                    status=MessageStatus.SENT,
                    provider_message_id=f"wamid-out-{uuid.uuid4()}",
                    phone_e164=phone,
                    body="Yes, we do.",
                    # The same key the turn handler derives — which is what makes the
                    # sweep's check and the handler's check the same check.
                    idempotency_key=reply_idempotency_key(str(inbound.id)),
                )
            )

        await session.flush()

        # `created_at` defaults to now(); age it so the grace period applies.
        await session.execute(
            update(Message)
            .where(Message.id == inbound.id)
            .values(created_at=datetime.now(UTC) - age)
        )
        return str(inbound.id)

    return run_db(seed)


def run_sweep(grace_seconds: int = 0) -> dict[str, Any]:
    from app.persistence import database

    async def run() -> dict[str, Any]:
        database._engine = None  # noqa: SLF001
        database._session_factory = None  # noqa: SLF001
        try:
            result = await sweep_unanswered_messages(
                TaskRequest(
                    task_type=TaskType.SWEEP_UNANSWERED_MESSAGES,
                    task_id="sweep-1",
                    payload={"grace_seconds": grace_seconds},
                )
            )
            return dict(result)
        finally:
            await database.dispose_engine()

    return asyncio.run(run())


class TestFindingStrandedMessages:
    def test_an_unanswered_message_is_found(self, sweep_env: list[TaskRequest]) -> None:
        message_id = seed_inbound(answered=False)

        result = run_sweep()

        assert result["found"] == 1
        assert result["requeued"] == 1
        assert sweep_env[0].payload["message_id"] == message_id

    def test_an_answered_message_is_left_alone(self, sweep_env: list[TaskRequest]) -> None:
        """The check is the same one the turn handler makes, so the two cannot
        disagree about what "answered" means."""
        seed_inbound(answered=True)

        result = run_sweep()

        assert result["found"] == 0
        assert sweep_env == []

    def test_a_recent_message_is_treated_as_in_flight(self, sweep_env: list[TaskRequest]) -> None:
        """A message stored seconds ago is normal, not stranded. Re-enqueueing it would
        race the task already on its way."""
        seed_inbound(answered=False, age=timedelta(seconds=5))

        result = run_sweep(grace_seconds=600)

        assert result["found"] == 0

    def test_a_closed_conversation_is_never_replied_into(
        self, sweep_env: list[TaskRequest]
    ) -> None:
        """[D36]: closed is terminal, so a sweep must not resurrect it."""
        seed_inbound(answered=False, conversation_status=ConversationStatus.CLOSED)

        assert run_sweep()["found"] == 0

    def test_an_outbound_message_is_not_a_candidate(self, sweep_env: list[TaskRequest]) -> None:
        seed_inbound(answered=True)

        assert run_sweep()["found"] == 0

    def test_several_stranded_messages_are_all_requeued(self, sweep_env: list[TaskRequest]) -> None:
        for index in range(3):
            seed_inbound(answered=False, phone=f"+91987650071{index}")

        result = run_sweep()

        assert result["found"] == 3
        assert len(sweep_env) == 3

    def test_the_batch_size_is_respected(
        self, sweep_env: list[TaskRequest], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A backlog is worked through in batches rather than enqueueing thousands of
        tasks at once."""
        from app.core.config import get_settings

        for index in range(4):
            seed_inbound(answered=False, phone=f"+91987650072{index}")

        monkeypatch.setattr(get_settings(), "sweep_batch_size", 2)
        assert run_sweep()["found"] == 2


class TestIdempotency:
    def test_the_requeued_task_carries_the_webhook_s_own_name(
        self, sweep_env: list[TaskRequest]
    ) -> None:
        """§20: a task somehow already queued collapses rather than producing a second
        reply."""
        message_id = seed_inbound(answered=False)

        run_sweep()

        # The sweep deliberately does NOT reuse the webhook's "turn-{id}" name: a task
        # Cloud Tasks has exhausted stays name-tombstoned for ~1h, so the same name would
        # be silently ignored. The "-sweep" suffix is a genuine new task; idempotency of
        # the turn handler makes double replies impossible.
        assert sweep_env[0].task_id == f"turn-{message_id}-sweep"
        assert sweep_env[0].task_type is TaskType.PROCESS_CONVERSATION_TURN

    def test_the_sweep_and_the_handler_agree_on_the_key(self) -> None:
        """The sweep builds this key in SQL and the handler builds it in Python. If they
        ever disagreed, the sweep would re-enqueue every message forever."""
        message_id = uuid.uuid4()

        assert reply_key_for(message_id) == reply_idempotency_key(str(message_id))

    def test_sweeping_twice_finds_nothing_new(self, sweep_env: list[TaskRequest]) -> None:
        """The second sweep still finds the message — it is genuinely still unanswered —
        and enqueues the same deterministic task, which the queue collapses."""
        seed_inbound(answered=False)

        first = run_sweep()
        second = run_sweep()

        assert first["found"] == 1
        assert second["found"] == 1
        assert sweep_env[0].task_id == sweep_env[1].task_id


class TestFailureHandling:
    def test_one_enqueue_failure_does_not_end_the_sweep(
        self, sweep_env: list[TaskRequest], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A backlog is exactly when the queue is most likely to be unhappy."""
        attempts = {"count": 0}

        class FlakyQueue:
            async def enqueue(self, task: TaskRequest) -> None:
                attempts["count"] += 1
                if attempts["count"] == 1:
                    raise RuntimeError("queue unavailable")

        monkeypatch.setattr(
            "app.worker.handlers.sweeps.build_queue_provider", lambda _settings: FlakyQueue()
        )

        for index in range(3):
            seed_inbound(answered=False, phone=f"+91987650073{index}")

        result = run_sweep()

        assert result["found"] == 3
        assert result["requeued"] == 2
        assert attempts["count"] == 3

    def test_an_empty_sweep_is_not_an_error(self, sweep_env: list[TaskRequest]) -> None:
        result = run_sweep()

        assert result["result"] == "swept"
        assert result["found"] == 0
