"""Bot-activity audit tests ([D67]).

Seeds conversations with known inbound shapes, runs the hourly sweep, and asserts
who gets flagged. Detection only: nothing here gates, pauses or answers a live
reply — the flag is a dashboard notification for human review.
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

from app.core.config import reset_settings_cache
from app.core.security import hash_password
from app.domain.enums import (
    ConversationStatus,
    LeadStatus,
    MessageDirection,
    MessageStatus,
    NotificationType,
    SuppressionReason,
    UserRole,
)
from app.integrations.queue import TaskRequest, TaskType
from app.persistence import database
from app.persistence.models import (
    Conversation,
    Lead,
    Message,
    Notification,
    User,
    WhatsAppSuppression,
)
from app.persistence.repositories import suppressions as suppression_repo
from app.worker.handlers.bot_audit import sweep_bot_activity
from tests.integration.helpers import ensure_product_id, get_test_database_url, run_db

pytestmark = pytest.mark.integration

PHONE = "+919876500300"


@pytest.fixture
def audit_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("DATABASE_URL", get_test_database_url())
    reset_settings_cache()
    database._engine = None  # noqa: SLF001
    database._session_factory = None  # noqa: SLF001

    async def clear(session: AsyncSession) -> None:
        await session.execute(delete(Notification))
        await session.execute(delete(Message))
        await session.execute(delete(Conversation))
        await session.execute(delete(Lead))
        await session.execute(delete(User))
        await session.execute(delete(WhatsAppSuppression))

    run_db(clear)
    yield
    run_db(clear)


def seed_conversation(
    *,
    phone: str = PHONE,
    messages: int,
    gap_seconds: int,
    lead_status: LeadStatus = LeadStatus.RESPONDED,
) -> tuple[str, str]:
    """A conversation with `messages` inbound turns spaced `gap_seconds` apart,
    ending now — all inside the audit's trailing hour unless the gap says otherwise."""

    async def create(session: AsyncSession) -> tuple[str, str]:
        session.add(
            User(
                email="reviewer@example.com",
                password_hash=hash_password("a-sufficiently-long-password"),
                role=UserRole.ADMIN,
                is_active=True,
            )
        )
        product_id = await ensure_product_id(session)
        lead = Lead(
            phone_e164=phone, full_name="Priya Nair", status=lead_status, product_id=product_id
        )
        session.add(lead)
        await session.flush()
        conversation = Conversation(
            lead_id=lead.id, status=ConversationStatus.ACTIVE, product_id=product_id
        )
        session.add(conversation)
        await session.flush()
        now = datetime.now(UTC)
        for index in range(messages):
            session.add(
                Message(
                    lead_id=lead.id,
                    conversation_id=conversation.id,
                    product_id=product_id,
                    direction=MessageDirection.INBOUND,
                    status=MessageStatus.DELIVERED,
                    provider_message_id=f"wamid-audit-{uuid.uuid4().hex}",
                    phone_e164=phone,
                    body="ping",
                    created_at=now - (messages - 1 - index) * timedelta(seconds=gap_seconds),
                )
            )
        await session.flush()
        return str(lead.id), str(conversation.id)

    return run_db(create)


def run_sweep() -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        database._engine = None  # noqa: SLF001
        database._session_factory = None  # noqa: SLF001
        try:
            return dict(
                await sweep_bot_activity(
                    TaskRequest(
                        task_type=TaskType.SWEEP_BOT_ACTIVITY,
                        task_id="sweep-bot-activity-test",
                        payload={},
                    )
                )
            )
        finally:
            await database.dispose_engine()

    return asyncio.run(run())


def notifications() -> list[Notification]:
    async def fetch(session: AsyncSession) -> list[Notification]:
        result = await session.execute(select(Notification))
        return list(result.scalars().all())

    return run_db(fetch)


class TestBotAuditSweep:
    def test_a_marathon_conversation_is_flagged(self, audit_env: None) -> None:
        """The staging signature: dozens of messages at machine-regular gaps."""
        seed_conversation(messages=35, gap_seconds=30)

        result = run_sweep()

        assert result["flagged"] == 1
        assert result["notified"] == 1
        assert result["blocked"] == 0
        [notification] = notifications()
        assert notification.type is NotificationType.BOT_SUSPECTED
        assert "Possible automated sender" in notification.title
        assert PHONE in notification.message
        assert notification.related_entity_type == "lead"

        async def suppressions(session: AsyncSession) -> int:
            return len((await session.execute(select(WhatsAppSuppression))).scalars().all())

        assert run_db(suppressions) == 0

    def test_a_repeat_run_collapses_instead_of_reflagging(self, audit_env: None) -> None:
        """The flag's unique constraint is per conversation: every hourly run
        re-flags the same marathon into zero new rows."""
        seed_conversation(messages=35, gap_seconds=30)

        run_sweep()
        result = run_sweep()

        assert result["flagged"] == 1
        assert result["notified"] == 0
        assert len(notifications()) == 1

    def test_an_unreviewed_repeat_escalates_to_a_block(self, audit_env: None) -> None:
        """Still suspect an hour later with the first flag unread by anyone: the
        team had its chance to look, so the audit blocks on its own — the same
        row `block-sender` writes, minus the human."""
        seed_conversation(messages=35, gap_seconds=30)

        run_sweep()
        result = run_sweep()

        assert result["blocked"] == 1

        async def block_reason(session: AsyncSession) -> SuppressionReason | None:
            return await suppression_repo.suppression_reason(session, PHONE)

        assert run_db(block_reason) is SuppressionReason.MANUAL

    def test_a_reviewed_repeat_is_never_overridden(self, audit_env: None) -> None:
        """A read flag means a human owns the decision. The audit keeps flagging
        silently but must not block over them."""
        seed_conversation(messages=35, gap_seconds=30)

        run_sweep()

        async def mark_read(session: AsyncSession) -> None:
            rows = (await session.execute(select(Notification))).scalars().all()
            for row in rows:
                row.is_read = True
                row.read_at = datetime.now(UTC)
            await session.commit()

        run_db(mark_read)
        result = run_sweep()

        assert result["flagged"] == 1
        assert result["blocked"] == 0

        async def suppressions(session: AsyncSession) -> int:
            return len((await session.execute(select(WhatsAppSuppression))).scalars().all())

        assert run_db(suppressions) == 0

    def test_blocking_can_be_turned_off_to_observe(
        self, audit_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Tune mode: flags without blocks while learning what trips in real
        traffic."""
        seed_conversation(messages=35, gap_seconds=30)

        run_sweep()

        monkeypatch.setenv("BOT_BLOCK_ON_REPEAT", "false")
        reset_settings_cache()
        result = run_sweep()

        assert result["flagged"] == 1
        assert result["blocked"] == 0

        async def suppressions(session: AsyncSession) -> int:
            return len((await session.execute(select(WhatsAppSuppression))).scalars().all())

        assert run_db(suppressions) == 0

    def test_an_ordinary_chat_is_clean(self, audit_env: None) -> None:
        seed_conversation(messages=6, gap_seconds=300)

        result = run_sweep()

        assert result == {
            "result": "audited",
            "conversations": 1,
            "flagged": 0,
            "notified": 0,
            "blocked": 0,
        }
        assert notifications() == []

    def test_a_suppressed_sender_is_skipped(self, audit_env: None) -> None:
        """Blocked or opted out means already handled — flagging it again would
        nag about a decided case."""
        seed_conversation(messages=35, gap_seconds=30)

        async def suppress(session: AsyncSession) -> None:
            session.add(
                WhatsAppSuppression(
                    phone_e164=PHONE,
                    reason=SuppressionReason.MANUAL,
                    notes="test block",
                )
            )
            await session.commit()

        run_db(suppress)

        result = run_sweep()

        assert result["flagged"] == 0
        assert notifications() == []

    def test_an_empty_audit_is_clean(self, audit_env: None) -> None:
        result = run_sweep()

        assert result["flagged"] == 0
        assert notifications() == []
