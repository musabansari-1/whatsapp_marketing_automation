"""First-reply welcome template (first message per phone gets a template, not AI text).

Placeholder-first: an unset selection disables the welcome and the turn runs the
normal AI path, so no product changes behaviour until an operator assigns a real
Meta-approved template and selects it. Nothing here invents a template — rows are
seeded as the Meta sync would store them, and selection goes through the same
ownership, approval and header-media guards campaigns use.

Nothing here reaches a network. Delivery is `FakeWhatsAppProvider`, so the
welcome is recorded, never sent.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import (
    ConversationStatus,
    LeadStatus,
    MessageDirection,
    MessageStatus,
    TemplateCategory,
    TemplateHeaderType,
)
from app.integrations.llm import FakeLLMProvider
from app.integrations.queue import TaskRequest, TaskType
from app.integrations.whatsapp import FakeWhatsAppProvider
from app.persistence.models import (
    Conversation,
    ConversationMessage,
    Lead,
    Message,
    Product,
    WhatsAppSuppression,
    WhatsAppTemplate,
)
from app.persistence.repositories import conversations as conversation_repo
from app.persistence.repositories import templates as template_repo
from tests.integration.conftest import login
from tests.integration.helpers import (
    ensure_product_id,
    get_test_database_url,
    run_db,
    scoped_prefix,
)

pytestmark = pytest.mark.integration

PHONE = "+919876500009"


@pytest.fixture
def welcome_env(
    engine: Any, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[FakeLLMProvider, FakeWhatsAppProvider]]:
    """Clean schema with both providers faked and inspectable."""
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
        "app.worker.handlers.conversation.build_whatsapp_provider",
        lambda _settings, **_: whatsapp,
    )

    async def clear(session: AsyncSession) -> None:
        from app.persistence.models import AIRun

        await session.execute(delete(AIRun))
        await session.execute(delete(ConversationMessage))
        await session.execute(delete(Message))
        await session.execute(delete(Conversation))
        await session.execute(delete(WhatsAppSuppression))
        await session.execute(delete(Lead))
        await session.execute(delete(WhatsAppTemplate))
        products = (await session.execute(select(Product))).scalars().all()
        for product in products:
            product.first_reply_template_id = None

    run_db(clear)
    yield llm, whatsapp
    run_db(clear)


def seed_inbound(*, phone: str = PHONE, body: str = "Hi, tell me more") -> tuple[str, str, str]:
    """One lead, one conversation and one stored inbound message."""

    async def seed(session: AsyncSession) -> tuple[str, str, str]:
        received_at = datetime.now(UTC)
        product_id = await ensure_product_id(session)

        lead = Lead(
            phone_e164=phone, full_name="Asha", status=LeadStatus.CONTACTED, product_id=product_id
        )
        session.add(lead)
        await session.flush()

        conversation = Conversation(
            lead_id=lead.id,
            status=ConversationStatus.ACTIVE,
            last_inbound_at=received_at,
            product_id=product_id,
        )
        session.add(conversation)
        await session.flush()

        message = Message(
            lead_id=lead.id,
            conversation_id=conversation.id,
            product_id=product_id,
            direction=MessageDirection.INBOUND,
            status=MessageStatus.DELIVERED,
            provider_message_id=f"wamid-{uuid.uuid4()}",
            phone_e164=phone,
            body=body,
            delivered_at=received_at,
        )
        session.add(message)
        await session.flush()
        return str(lead.id), str(conversation.id), str(message.id)

    return run_db(seed)


def seed_template(
    *,
    product_id: uuid.UUID,
    name: str = "welcome",
    status: str = "APPROVED",
    header_type: TemplateHeaderType = TemplateHeaderType.NONE,
    variable_count: int = 1,
) -> str:
    """A template row as the Meta sync stores it, already assigned to the product."""

    async def insert(session: AsyncSession) -> str:
        template = WhatsAppTemplate(
            name=name,
            language="en",
            product_id=product_id,
            provider_template_id=f"tpl-{uuid.uuid4()}",
            provider_status=status,
            category=TemplateCategory.MARKETING,
            variable_count=variable_count,
            body_preview="Hello {{1}}, welcome!",
            header_type=header_type,
            header_media_url="https://example.invalid/welcome.png"
            if header_type.needs_media
            else None,
        )
        session.add(template)
        await session.flush()
        return str(template.id)

    return run_db(insert)


def select_first_reply(product_id: uuid.UUID, template_id: str) -> None:
    async def select_template(session: AsyncSession) -> None:
        await template_repo.set_first_reply_template(
            session, product_id=product_id, template_id=uuid.UUID(template_id)
        )

    run_db(select_template)


def seed_followup(lead_id: str, conversation_id: str, *, body: str) -> str:
    """A second stored inbound message on the same lead and conversation."""

    async def seed(session: AsyncSession) -> str:
        received_at = datetime.now(UTC)
        lead = await session.get(Lead, uuid.UUID(lead_id))
        conversation = await session.get(Conversation, uuid.UUID(conversation_id))
        assert lead is not None and conversation is not None
        message = Message(
            lead_id=lead.id,
            conversation_id=conversation.id,
            product_id=conversation.product_id,
            direction=MessageDirection.INBOUND,
            status=MessageStatus.DELIVERED,
            provider_message_id=f"wamid-{uuid.uuid4()}",
            phone_e164=lead.phone_e164,
            body=body,
            delivered_at=received_at,
        )
        session.add(message)
        conversation.last_inbound_at = received_at
        await session.flush()
        return str(message.id)

    return run_db(seed)


def product_of_lead(lead_id: str) -> uuid.UUID:
    async def read(session: AsyncSession) -> uuid.UUID:
        lead = await session.get(Lead, uuid.UUID(lead_id))
        assert lead is not None
        return lead.product_id

    return run_db(read)


def make_product(slug: str) -> uuid.UUID:
    async def create(session: AsyncSession) -> uuid.UUID:
        product = Product(slug=slug, display_name=slug.upper())
        session.add(product)
        await session.flush()
        return product.id

    return run_db(create)


def run_turn(message_id: str) -> dict[str, Any]:
    """Run the handler on its own event loop, as the worker would."""
    import asyncio

    from app.persistence import database
    from app.worker.handlers.conversation import process_conversation_turn

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


def outbound_rows(conversation_id: str) -> list[Message]:
    async def fetch(session: AsyncSession) -> list[Message]:
        result = await session.execute(
            select(Message).where(
                Message.conversation_id == uuid.UUID(conversation_id),
                Message.direction == MessageDirection.OUTBOUND,
            )
        )
        return list(result.scalars().all())

    return run_db(fetch)


def ai_run_count() -> int:
    from app.persistence.models import AIRun

    async def fetch(session: AsyncSession) -> int:
        result = await session.execute(select(AIRun))
        return len(list(result.scalars().all()))

    return run_db(fetch)


class TestPlaceholderIsToday:
    def test_no_selection_sends_ai_text(
        self, welcome_env: tuple[Any, FakeWhatsAppProvider]
    ) -> None:
        _, whatsapp = welcome_env
        _, conversation_id, message_id = seed_inbound()

        outcome = run_turn(message_id)

        assert outcome["result"] == "replied"
        assert len(whatsapp.sent_texts) == 1
        assert whatsapp.sent_templates == []


class TestFirstReplyTemplate:
    def test_first_message_sends_the_template_not_ai_text(
        self, welcome_env: tuple[FakeLLMProvider, FakeWhatsAppProvider]
    ) -> None:
        _, whatsapp = welcome_env
        lead_id, conversation_id, message_id = seed_inbound()
        template_id = seed_template(product_id=product_of_lead(lead_id))
        select_first_reply(product_of_lead(lead_id), template_id)

        outcome = run_turn(message_id)

        assert outcome["result"] == "first_reply_template"
        assert outcome["replied"] is True
        assert len(whatsapp.sent_templates) == 1
        assert whatsapp.sent_texts == []
        sent = whatsapp.sent_templates[0]
        assert sent.template_name == "welcome"
        assert sent.variables == ("Asha",)
        # No model call: the welcome costs a template send, not LLM quota.
        assert ai_run_count() == 0

    def test_welcome_is_recorded_and_advances_the_thread(
        self, welcome_env: tuple[Any, FakeWhatsAppProvider]
    ) -> None:
        _, _ = welcome_env
        lead_id, conversation_id, message_id = seed_inbound()
        template_id = seed_template(product_id=product_of_lead(lead_id))
        select_first_reply(product_of_lead(lead_id), template_id)

        run_turn(message_id)

        assert transcript(conversation_id) == [
            ("user", "Hi, tell me more"),
            ("assistant", "Hello {{1}}, welcome!"),
        ]
        rows = outbound_rows(conversation_id)
        assert len(rows) == 1
        assert rows[0].template_name == "welcome"
        assert rows[0].idempotency_key == f"reply-{message_id}"

        async def read(session: AsyncSession) -> tuple[LeadStatus, ConversationStatus]:
            lead = await session.get(Lead, uuid.UUID(lead_id))
            conversation = await session.get(Conversation, uuid.UUID(conversation_id))
            assert lead is not None and conversation is not None
            return lead.status, conversation.status

        assert run_db(read) == (LeadStatus.RESPONDED, ConversationStatus.WAITING_FOR_USER)

    def test_second_message_gets_ai_text(
        self, welcome_env: tuple[Any, FakeWhatsAppProvider]
    ) -> None:
        _, whatsapp = welcome_env
        lead_id, conversation_id, first_id = seed_inbound()
        template_id = seed_template(product_id=product_of_lead(lead_id))
        select_first_reply(product_of_lead(lead_id), template_id)
        run_turn(first_id)
        second_id = seed_followup(lead_id, conversation_id, body="And the price?")

        outcome = run_turn(second_id)

        assert outcome["result"] == "replied"
        assert len(whatsapp.sent_templates) == 1
        assert len(whatsapp.sent_texts) == 1

    def test_broadcast_history_does_not_count_as_replied(
        self, welcome_env: tuple[Any, FakeWhatsAppProvider]
    ) -> None:
        """A campaign row carries no conversation, so a broadcast recipient is unreached."""
        _, whatsapp = welcome_env
        lead_id, conversation_id, message_id = seed_inbound()
        template_id = seed_template(product_id=product_of_lead(lead_id))
        select_first_reply(product_of_lead(lead_id), template_id)

        async def seed_broadcast(session: AsyncSession) -> None:
            lead = await session.get(Lead, uuid.UUID(lead_id))
            assert lead is not None
            session.add(
                Message(
                    lead_id=lead.id,
                    conversation_id=None,
                    product_id=lead.product_id,
                    direction=MessageDirection.OUTBOUND,
                    status=MessageStatus.SENT,
                    provider_message_id=f"wamid-{uuid.uuid4()}",
                    phone_e164=lead.phone_e164,
                    template_name="campaign-blast",
                    body="Campaign blast",
                    idempotency_key=f"send-{uuid.uuid4()}",
                    sent_at=datetime.now(UTC),
                )
            )

        run_db(seed_broadcast)

        outcome = run_turn(message_id)

        assert outcome["result"] == "first_reply_template"
        assert len(whatsapp.sent_templates) == 1

    def test_retry_of_the_same_message_sends_nothing_again(
        self, welcome_env: tuple[Any, FakeWhatsAppProvider]
    ) -> None:
        _, whatsapp = welcome_env
        lead_id, _, message_id = seed_inbound()
        template_id = seed_template(product_id=product_of_lead(lead_id))
        select_first_reply(product_of_lead(lead_id), template_id)

        assert run_turn(message_id)["result"] == "first_reply_template"
        assert run_turn(message_id)["result"] == "already_replied"
        assert len(whatsapp.sent_templates) == 1

    def test_suppressed_sender_gets_no_welcome(
        self, welcome_env: tuple[Any, FakeWhatsAppProvider]
    ) -> None:
        _, whatsapp = welcome_env
        lead_id, _, message_id = seed_inbound()
        template_id = seed_template(product_id=product_of_lead(lead_id))
        select_first_reply(product_of_lead(lead_id), template_id)

        async def suppress(session: AsyncSession) -> None:
            lead = await session.get(Lead, uuid.UUID(lead_id))
            assert lead is not None
            session.add(WhatsAppSuppression(phone_e164=lead.phone_e164, lead_id=lead.id))

        run_db(suppress)

        assert run_turn(message_id)["result"] == "suppressed"
        assert whatsapp.total_sent == 0


class TestMisconfigurationFallsBackToAI:
    def test_unapproved_template_falls_back_to_ai_text(
        self, welcome_env: tuple[Any, FakeWhatsAppProvider]
    ) -> None:
        _, whatsapp = welcome_env
        lead_id, _, message_id = seed_inbound()
        product_id = product_of_lead(lead_id)
        template_id = seed_template(product_id=product_id, status="PENDING")

        async def force_select(session: AsyncSession) -> None:
            product = await session.get(Product, product_id)
            assert product is not None
            product.first_reply_template_id = uuid.UUID(template_id)

        run_db(force_select)

        assert run_turn(message_id)["result"] == "replied"
        assert whatsapp.sent_templates == []
        assert len(whatsapp.sent_texts) == 1

    def test_media_header_without_asset_falls_back_to_ai_text(
        self, welcome_env: tuple[Any, FakeWhatsAppProvider]
    ) -> None:
        _, whatsapp = welcome_env
        lead_id, _, message_id = seed_inbound()
        product_id = product_of_lead(lead_id)

        async def seed_broken(session: AsyncSession) -> None:
            template = WhatsAppTemplate(
                name="welcome",
                language="en",
                product_id=product_id,
                provider_template_id=f"tpl-{uuid.uuid4()}",
                provider_status="APPROVED",
                category=TemplateCategory.MARKETING,
                variable_count=0,
                body_preview="Welcome!",
                header_type=TemplateHeaderType.IMAGE,
                header_media_url=None,
            )
            session.add(template)
            await session.flush()
            product = await session.get(Product, product_id)
            assert product is not None
            product.first_reply_template_id = template.id

        run_db(seed_broken)

        assert run_turn(message_id)["result"] == "replied"
        assert whatsapp.sent_templates == []
        assert len(whatsapp.sent_texts) == 1


def try_set(product_id: uuid.UUID, template_id: str) -> str:
    """Set the welcome, returning the outcome: 'ok' or the refusal type name."""

    async def attempt(session: AsyncSession) -> str:
        from app.domain.campaign import (
            TemplateAssignmentError,
            TemplateNotApprovedError,
            TemplateNotFoundError,
            TemplateOwnershipError,
        )

        try:
            await template_repo.set_first_reply_template(
                session, product_id=product_id, template_id=uuid.UUID(template_id)
            )
        except (
            TemplateAssignmentError,
            TemplateNotApprovedError,
            TemplateNotFoundError,
            TemplateOwnershipError,
        ) as exc:
            return type(exc).__name__
        return "ok"

    return run_db(attempt)


class TestSelectionGuards:
    def test_set_refuses_foreign_template(
        self, welcome_env: tuple[Any, FakeWhatsAppProvider]
    ) -> None:
        _, _ = welcome_env
        lead_id, _, _ = seed_inbound()
        product_id = product_of_lead(lead_id)
        owned = seed_template(product_id=product_id, name="owned")
        foreign = seed_template(
            product_id=make_product(f"other-{uuid.uuid4().hex[:6]}"), name="foreign"
        )

        assert try_set(product_id, owned) == "ok"
        assert try_set(product_id, foreign) == "TemplateOwnershipError"

    def test_set_refuses_unapproved_or_media_less_template(
        self, welcome_env: tuple[Any, FakeWhatsAppProvider]
    ) -> None:
        _, _ = welcome_env
        lead_id, _, _ = seed_inbound()
        product_id = product_of_lead(lead_id)
        pending = seed_template(product_id=product_id, name="pending", status="PENDING")
        assert try_set(product_id, pending) == "TemplateNotApprovedError"

        async def seed_media_less(session: AsyncSession) -> str:
            template = WhatsAppTemplate(
                name="media-less",
                language="en",
                product_id=product_id,
                provider_template_id=f"tpl-{uuid.uuid4()}",
                provider_status="APPROVED",
                category=TemplateCategory.MARKETING,
                variable_count=0,
                body_preview="Welcome!",
                header_type=TemplateHeaderType.IMAGE,
                header_media_url=None,
            )
            session.add(template)
            await session.flush()
            return str(template.id)

        assert try_set(product_id, run_db(seed_media_less)) == "TemplateAssignmentError"


class TestFirstReplyAPI:
    @pytest.fixture
    def admin(
        self, client: TestClient, seeded_users: dict[str, str], seeded_product: uuid.UUID
    ) -> TestClient:
        login(client)
        return client

    def _clean_templates(self) -> None:
        async def clear(session: AsyncSession) -> None:
            await session.execute(delete(WhatsAppTemplate))
            products = (await session.execute(select(Product))).scalars().all()
            for product in products:
                product.first_reply_template_id = None

        run_db(clear)

    def test_set_get_and_clear(self, admin: TestClient, seeded_product: uuid.UUID) -> None:
        self._clean_templates()
        template_id = seed_template(product_id=seeded_product)
        prefix = scoped_prefix(seeded_product)

        assert admin.get(f"{prefix}/first-reply-template").status_code == 404

        response = admin.put(f"{prefix}/first-reply-template/{template_id}")
        assert response.status_code == 200, response.text

        assert admin.get(f"{prefix}/first-reply-template").status_code == 200

        assert admin.delete(f"{prefix}/first-reply-template").status_code == 204
        assert admin.get(f"{prefix}/first-reply-template").status_code == 404

    def test_set_unowned_template_is_404(
        self, admin: TestClient, seeded_product: uuid.UUID
    ) -> None:
        self._clean_templates()
        foreign = seed_template(
            product_id=make_product(f"other-{uuid.uuid4().hex[:6]}"), name="foreign"
        )
        prefix = scoped_prefix(seeded_product)

        assert admin.put(f"{prefix}/first-reply-template/{foreign}").status_code == 404

    def test_set_unapproved_template_is_409(
        self, admin: TestClient, seeded_product: uuid.UUID
    ) -> None:
        self._clean_templates()
        pending = seed_template(product_id=seeded_product, name="pending", status="PENDING")
        prefix = scoped_prefix(seeded_product)

        assert admin.put(f"{prefix}/first-reply-template/{pending}").status_code == 409
