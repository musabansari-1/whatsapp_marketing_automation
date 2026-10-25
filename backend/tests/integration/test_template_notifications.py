"""Templates sent on behalf of a product's own backend (`/integrations/.../template-messages`).

Against real PostgreSQL with WhatsApp faked: nothing leaves. The API half checks the
token and the request and queues; the worker half sends once, records the message in
the inbox, and still refuses a number that opted out after the request.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_queue_provider
from app.domain.enums import (
    ConversationStatus,
    MessageDirection,
    SuppressionReason,
    TemplateCategory,
)
from app.integrations.queue import EnqueuedTask, TaskRequest, TaskType
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
from app.worker.handlers.template_notification import render_template_body
from tests.integration.helpers import ensure_product_id, get_test_database_url, run_db

pytestmark = pytest.mark.integration

TOKEN = "integration-test-token"  # noqa: S105 - test fixture, not a credential
PHONE = "9876500011"
PHONE_E164 = "+919876500011"
APPROVED_BODY = "Hello {{1}}, your LifeHerba profile is approved and live."


async def _clear(session: AsyncSession) -> None:
    await session.execute(delete(ConversationMessage))
    await session.execute(delete(Message))
    await session.execute(delete(Conversation))
    await session.execute(delete(WhatsAppSuppression))
    await session.execute(delete(Lead))
    products = (await session.execute(select(Product))).scalars().all()
    for product in products:
        product.first_reply_template_id = None
    await session.flush()
    await session.execute(delete(WhatsAppTemplate))


@pytest.fixture
def product(engine: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[uuid.UUID, str]]:
    """The fixture product, active, with a token configured for its slug.

    Its previous active flag is put back afterwards: later suites expect the shared
    fixture product as they left it.
    """

    async def prepare(session: AsyncSession) -> tuple[uuid.UUID, str, bool]:
        await _clear(session)
        product_id = await ensure_product_id(session)
        row = await session.get(Product, product_id)
        assert row is not None
        was_active = row.is_active
        row.is_active = True
        return product_id, row.slug, was_active

    product_id, slug, was_active = run_db(prepare)
    monkeypatch.setenv("INTEGRATION_TOKENS", json.dumps({slug: TOKEN}))
    yield product_id, slug

    async def restore(session: AsyncSession) -> None:
        await _clear(session)
        row = await session.get(Product, product_id)
        if row is not None:
            row.is_active = was_active

    run_db(restore)


def seed_template(
    product_id: uuid.UUID, *, name: str = "lifeherba_provider_approved", status: str = "APPROVED"
) -> str:
    async def insert(session: AsyncSession) -> str:
        template = WhatsAppTemplate(
            name=name,
            language="en",
            product_id=product_id,
            provider_template_id=f"tpl-{uuid.uuid4()}",
            provider_status=status,
            category=TemplateCategory.UTILITY,
            variable_count=1,
            body_preview=APPROVED_BODY,
        )
        session.add(template)
        await session.flush()
        return str(template.id)

    return run_db(insert)


def suppress(phone: str) -> None:
    async def insert(session: AsyncSession) -> None:
        session.add(WhatsAppSuppression(phone_e164=phone, reason=SuppressionReason.OPT_OUT_KEYWORD))

    run_db(insert)


def request_body(**overrides: Any) -> dict[str, Any]:
    return {
        "phone": PHONE,
        "full_name": "Dr. Asha",
        "template_name": "lifeherba_provider_approved",
        "variables": ["Dr. Asha"],
        "idempotency_key": "job-1",
        **overrides,
    }


class RecordingQueue:
    def __init__(self) -> None:
        self.tasks: list[TaskRequest] = []

    async def enqueue(self, task: TaskRequest) -> EnqueuedTask:
        self.tasks.append(task)
        return EnqueuedTask(task_id=task.task_id, task_type=task.task_type)


class TestTemplateMessageApi:
    @pytest.fixture
    def queue(self, product: tuple[uuid.UUID, str], client: TestClient) -> RecordingQueue:
        recording = RecordingQueue()
        client.app.dependency_overrides[get_queue_provider] = lambda: recording  # type: ignore[attr-defined]
        return recording

    def post(
        self, client: TestClient, slug: str, body: dict[str, Any], token: str | None = TOKEN
    ) -> Any:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return client.post(
            f"/api/v1/integrations/{slug}/template-messages", json=body, headers=headers
        )

    def test_an_approved_template_is_queued_for_the_normalised_number(
        self, product: tuple[uuid.UUID, str], client: TestClient, queue: RecordingQueue
    ) -> None:
        product_id, slug = product
        template_id = seed_template(product_id)

        response = self.post(client, slug, request_body())

        assert response.status_code == 202, response.text
        assert [t.task_type for t in queue.tasks] == [TaskType.SEND_TEMPLATE_NOTIFICATION]
        payload = queue.tasks[0].payload
        assert payload["phone_e164"] == PHONE_E164
        assert payload["template_id"] == template_id
        assert payload["variables"] == ["Dr. Asha"]

    def test_the_same_key_queues_under_the_same_task_id(
        self, product: tuple[uuid.UUID, str], client: TestClient, queue: RecordingQueue
    ) -> None:
        product_id, slug = product
        seed_template(product_id)

        self.post(client, slug, request_body())
        self.post(client, slug, request_body())

        assert len({t.task_id for t in queue.tasks}) == 1

    @pytest.mark.parametrize("token", [None, "wrong-token"])
    def test_a_missing_or_wrong_token_is_401(
        self,
        product: tuple[uuid.UUID, str],
        client: TestClient,
        queue: RecordingQueue,
        token: str | None,
    ) -> None:
        product_id, slug = product
        seed_template(product_id)

        assert self.post(client, slug, request_body(), token=token).status_code == 401
        assert queue.tasks == []

    def test_a_token_reaches_only_its_own_product(
        self, product: tuple[uuid.UUID, str], client: TestClient, queue: RecordingQueue
    ) -> None:
        assert self.post(client, "some-other-product", request_body()).status_code == 401

    def test_an_unapproved_or_unknown_template_is_422(
        self, product: tuple[uuid.UUID, str], client: TestClient, queue: RecordingQueue
    ) -> None:
        product_id, slug = product
        seed_template(product_id, status="PENDING")

        assert self.post(client, slug, request_body()).status_code == 422
        unknown = request_body(template_name="lifeherba_unknown")
        assert self.post(client, slug, unknown).status_code == 422
        assert queue.tasks == []

    def test_the_wrong_number_of_variables_is_422(
        self, product: tuple[uuid.UUID, str], client: TestClient, queue: RecordingQueue
    ) -> None:
        product_id, slug = product
        seed_template(product_id)

        response = self.post(client, slug, request_body(variables=["Dr. Asha", "extra"]))

        assert response.status_code == 422
        assert "takes 1 variables" in response.json()["detail"]

    def test_a_variable_meta_would_refuse_is_422(
        self, product: tuple[uuid.UUID, str], client: TestClient, queue: RecordingQueue
    ) -> None:
        _product_id, slug = product

        response = self.post(client, slug, request_body(variables=["two\nlines"]))

        assert response.status_code == 422

    def test_an_opted_out_number_is_409(
        self, product: tuple[uuid.UUID, str], client: TestClient, queue: RecordingQueue
    ) -> None:
        product_id, slug = product
        seed_template(product_id)
        suppress(PHONE_E164)

        assert self.post(client, slug, request_body()).status_code == 409
        assert queue.tasks == []


def run_notification(product_id: uuid.UUID, template_id: str, key: str = "job-1") -> str:
    from app.persistence import database
    from app.worker.handlers.template_notification import send_template_notification

    async def run() -> str:
        database._engine = None  # noqa: SLF001
        database._session_factory = None  # noqa: SLF001
        try:
            result = await send_template_notification(
                TaskRequest(
                    task_type=TaskType.SEND_TEMPLATE_NOTIFICATION,
                    task_id=f"template-notification-{key}",
                    payload={
                        "product_id": str(product_id),
                        "template_id": template_id,
                        "phone_e164": PHONE_E164,
                        "full_name": "Dr. Asha",
                        "variables": ["Dr. Asha"],
                        "idempotency_key": key,
                    },
                )
            )
            return str(result["result"])
        finally:
            await database.dispose_engine()

    return asyncio.run(run())


class TestTemplateNotificationWorker:
    @pytest.fixture
    def whatsapp(
        self, product: tuple[uuid.UUID, str], monkeypatch: pytest.MonkeyPatch
    ) -> FakeWhatsAppProvider:
        from app.core.config import reset_settings_cache

        monkeypatch.setenv("DATABASE_URL", get_test_database_url())
        reset_settings_cache()
        fake = FakeWhatsAppProvider()
        monkeypatch.setattr(
            "app.worker.handlers.template_notification.build_whatsapp_provider",
            lambda _settings, **_: fake,
        )
        return fake

    def test_it_sends_once_and_records_the_message_in_the_inbox(
        self, product: tuple[uuid.UUID, str], whatsapp: FakeWhatsAppProvider
    ) -> None:
        product_id, _slug = product
        template_id = seed_template(product_id)

        assert run_notification(product_id, template_id) == "sent"
        assert run_notification(product_id, template_id) == "already_sent"

        assert [(m.to_e164, m.template_name, m.variables) for m in whatsapp.sent_templates] == [
            (PHONE_E164, "lifeherba_provider_approved", ("Dr. Asha",))
        ]

        async def inbox(session: AsyncSession) -> tuple[Lead, Conversation, list[Any]]:
            lead = (
                await session.execute(select(Lead).where(Lead.phone_e164 == PHONE_E164))
            ).scalar_one()
            conversation = (
                await session.execute(select(Conversation).where(Conversation.lead_id == lead.id))
            ).scalar_one()
            turns = await conversation_repo.get_history(session, conversation.id)
            return lead, conversation, list(turns)

        lead, conversation, turns = run_db(inbox)
        assert lead.full_name == "Dr. Asha"
        assert conversation.status is ConversationStatus.WAITING_FOR_USER
        assert [(t.role, t.content) for t in turns] == [
            ("assistant", "Hello Dr. Asha, your LifeHerba profile is approved and live.")
        ]
        assert turns[0].tool_calls == {
            "sent_by": "system",
            "template": "lifeherba_provider_approved",
        }

        async def outbound(session: AsyncSession) -> list[Message]:
            result = await session.execute(
                select(Message).where(Message.direction == MessageDirection.OUTBOUND)
            )
            return list(result.scalars().all())

        assert [m.template_name for m in run_db(outbound)] == ["lifeherba_provider_approved"]

    def test_a_number_that_opted_out_after_the_request_is_not_messaged(
        self, product: tuple[uuid.UUID, str], whatsapp: FakeWhatsAppProvider
    ) -> None:
        product_id, _slug = product
        template_id = seed_template(product_id)
        suppress(PHONE_E164)

        assert run_notification(product_id, template_id) == "suppressed"
        assert whatsapp.sent_templates == []

    def test_a_template_paused_after_the_request_is_not_sent(
        self, product: tuple[uuid.UUID, str], whatsapp: FakeWhatsAppProvider
    ) -> None:
        product_id, _slug = product
        template_id = seed_template(product_id, status="PAUSED")

        assert run_notification(product_id, template_id) == "template_unusable"
        assert whatsapp.sent_templates == []


class TestRenderTemplateBody:
    def test_placeholders_are_filled_in_order(self) -> None:
        assert render_template_body("Hi {{1}}, need {{2}}.", ["Asha", "a licence"]) == (
            "Hi Asha, need a licence."
        )

    def test_a_missing_variable_stays_as_written(self) -> None:
        assert render_template_body("Hi {{1}} {{2}}", ["Asha"]) == "Hi Asha {{2}}"

    def test_no_preview_is_an_empty_body(self) -> None:
        assert render_template_body(None, ["Asha"]) == ""


class TestTemplateSyncApi:
    """The dashboard's "Sync from Meta" button."""

    def test_an_admin_queues_a_sync(self, seeded_users: dict[str, str], client: TestClient) -> None:
        from tests.integration.conftest import login

        recording = RecordingQueue()
        client.app.dependency_overrides[get_queue_provider] = lambda: recording  # type: ignore[attr-defined]
        login(client)

        response = client.post("/api/v1/whatsapp-templates/sync")

        assert response.status_code == 202, response.text
        assert [t.task_type for t in recording.tasks] == [TaskType.SYNC_WHATSAPP_TEMPLATES]

    def test_a_salesperson_cannot(self, seeded_users: dict[str, str], client: TestClient) -> None:
        from tests.integration.conftest import SALES_EMAIL, login

        recording = RecordingQueue()
        client.app.dependency_overrides[get_queue_provider] = lambda: recording  # type: ignore[attr-defined]
        login(client, SALES_EMAIL)

        assert client.post("/api/v1/whatsapp-templates/sync").status_code == 403
        assert recording.tasks == []
