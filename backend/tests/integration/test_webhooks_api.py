"""Webhook endpoint and processing tests (§8, §19, §20, [D5], [D16]).

Covers the whole inbound path: Meta posts, the endpoint verifies and persists, and
the worker handler applies the consequences.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.main import API_V1_PREFIX, create_app
from app.core.config import Settings, reset_settings_cache
from app.domain.enums import (
    LeadStatus,
    MessageStatus,
    RecipientStatus,
    WebhookEventStatus,
)
from app.integrations.queue import TaskRequest, TaskType
from app.persistence.models import (
    Campaign,
    CampaignRecipient,
    Conversation,
    ConversationMessage,
    Lead,
    Message,
    WebhookEvent,
    WhatsAppSuppression,
)
from app.worker.handlers.whatsapp import process_inbound_webhook
from tests.integration.helpers import get_test_database_url, run_db

pytestmark = pytest.mark.integration

APP_SECRET = "webhook-app-secret"  # noqa: S105 - test fixture
VERIFY_TOKEN = "verify-me"  # noqa: S105 - test fixture
WRONG_SECRET = "not-the-secret"  # noqa: S105 - test fixture
CUSTOMER = "+919876543210"


@pytest.fixture
def webhook_client(engine: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """An app configured with webhook credentials.

    The WhatsApp provider stays fake, so nothing here can send a real message.
    """
    from app.api.deps import reset_queue_provider
    from app.persistence import database

    monkeypatch.setenv("DATABASE_URL", get_test_database_url())
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    monkeypatch.setenv("WHATSAPP_APP_SECRET", APP_SECRET)
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", VERIFY_TOKEN)
    reset_settings_cache()
    reset_queue_provider()
    database._engine = None  # noqa: SLF001
    database._session_factory = None  # noqa: SLF001

    async def clear(session: AsyncSession) -> None:
        await session.execute(delete(ConversationMessage))
        await session.execute(delete(Message))
        await session.execute(delete(Conversation))
        await session.execute(delete(CampaignRecipient))
        await session.execute(delete(Campaign))
        await session.execute(delete(WhatsAppSuppression))
        await session.execute(delete(Lead))
        await session.execute(delete(WebhookEvent))

    run_db(clear)
    with TestClient(create_app(Settings())) as client:
        yield client

    run_db(clear)
    database._engine = None  # noqa: SLF001
    database._session_factory = None  # noqa: SLF001
    reset_queue_provider()
    reset_settings_cache()


def post_webhook(
    client: TestClient, payload: dict[str, Any], *, secret: str = APP_SECRET
) -> Response:
    body = json.dumps(payload).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    response: Response = client.post(
        f"{API_V1_PREFIX}/webhooks/whatsapp",
        content=body,
        headers={
            "X-Hub-Signature-256": f"sha256={signature}",
            "Content-Type": "application/json",
        },
    )
    return response


def inbound(text: str, message_id: str = "wamid.1", sender: str = "919876543210") -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba-1",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "contacts": [{"wa_id": sender, "profile": {"name": "Rahul"}}],
                            "messages": [
                                {
                                    "id": message_id,
                                    "from": sender,
                                    "timestamp": "1788000000",
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def status_update(message_id: str, status: str) -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba-1",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "statuses": [
                                {
                                    "id": message_id,
                                    "status": status,
                                    "timestamp": "1788000100",
                                    "recipient_id": "919876543210",
                                }
                            ]
                        },
                    }
                ],
            }
        ],
    }


def process_all() -> None:
    """Run the worker handler for every unprocessed webhook event."""

    async def pending(session: AsyncSession) -> list[str]:
        rows = (
            (
                await session.execute(
                    select(WebhookEvent.id).where(
                        WebhookEvent.status == WebhookEventStatus.RECEIVED
                    )
                )
            )
            .scalars()
            .all()
        )
        return [str(row) for row in rows]

    for event_id in run_db(pending):
        run_db_task(event_id)


def run_db_task(event_id: str) -> None:
    """Run the worker handler on its own event loop.

    The handler resolves the database through the module-level singletons, which the
    TestClient already built on *its* loop. asyncpg connections are bound to the loop
    that created them, so the singletons are reset first — forcing the handler to
    build its own engine here — and disposed afterwards so the app rebuilds on its own
    loop for the next request.
    """
    import asyncio

    from app.persistence import database

    async def run() -> None:
        database._engine = None  # noqa: SLF001
        database._session_factory = None  # noqa: SLF001
        try:
            await process_inbound_webhook(
                TaskRequest(
                    task_type=TaskType.PROCESS_INBOUND_MESSAGE,
                    payload={"webhook_event_id": event_id},
                )
            )
        finally:
            await database.dispose_engine()

    asyncio.run(run())


class TestSubscriptionVerification:
    def test_a_valid_handshake_echoes_the_challenge(self, webhook_client: TestClient) -> None:
        response = webhook_client.get(
            f"{API_V1_PREFIX}/webhooks/whatsapp",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": VERIFY_TOKEN,
                "hub.challenge": "1158201444",
            },
        )
        assert response.status_code == 200
        assert response.text == "1158201444"

    def test_a_wrong_token_is_rejected(self, webhook_client: TestClient) -> None:
        response = webhook_client.get(
            f"{API_V1_PREFIX}/webhooks/whatsapp",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "wrong",
                "hub.challenge": "123",
            },
        )
        assert response.status_code == 403


class TestSignatureEnforcement:
    def test_an_unsigned_delivery_is_rejected(self, webhook_client: TestClient) -> None:
        """§19: without this, anyone could fabricate customer replies and opt-outs."""
        response = webhook_client.post(f"{API_V1_PREFIX}/webhooks/whatsapp", json=inbound("Hello"))
        assert response.status_code == 403

    def test_a_wrong_signature_is_rejected(self, webhook_client: TestClient) -> None:
        response = post_webhook(webhook_client, inbound("Hi"), secret=WRONG_SECRET)
        assert response.status_code == 403

    def test_a_rejected_delivery_is_not_persisted(self, webhook_client: TestClient) -> None:
        post_webhook(webhook_client, inbound("Hi"), secret=WRONG_SECRET)

        async def count(session: AsyncSession) -> int:
            return len((await session.execute(select(WebhookEvent))).scalars().all())

        assert run_db(count) == 0

    def test_the_webhook_needs_no_session_cookie(self, webhook_client: TestClient) -> None:
        """Meta calls this directly, bypassing the BFF ([D13]), so it authenticates
        by signature rather than by session."""
        assert post_webhook(webhook_client, inbound("Hello")).status_code == 200


class TestFastAcknowledgement:
    def test_a_valid_delivery_is_accepted_and_stored(self, webhook_client: TestClient) -> None:
        response = post_webhook(webhook_client, inbound("Hello"))
        assert response.status_code == 200
        assert response.json()["status"] == "accepted"

        async def events(session: AsyncSession) -> int:
            return len((await session.execute(select(WebhookEvent))).scalars().all())

        assert run_db(events) == 1

    def test_the_webhook_does_no_processing_itself(self, webhook_client: TestClient) -> None:
        """§8: persist and queue, return quickly. The lead appears only after the
        worker runs."""
        post_webhook(webhook_client, inbound("Hello"))

        async def leads(session: AsyncSession) -> int:
            return len((await session.execute(select(Lead))).scalars().all())

        assert run_db(leads) == 0

    def test_a_redelivery_is_reported_as_a_duplicate(self, webhook_client: TestClient) -> None:
        """§8 duplicate protection. Meta redelivers routinely, and a repeat must be a
        success — an error would only make it redeliver again."""
        assert post_webhook(webhook_client, inbound("Hello")).json()["status"] == "accepted"
        assert post_webhook(webhook_client, inbound("Hello")).json()["status"] == "duplicate"

        async def events(session: AsyncSession) -> int:
            return len((await session.execute(select(WebhookEvent))).scalars().all())

        assert run_db(events) == 1

    def test_an_unmodelled_payload_is_still_accepted(self, webhook_client: TestClient) -> None:
        """Returning an error for something we simply do not model would produce an
        endless Meta retry loop."""
        response = post_webhook(
            webhook_client,
            {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {}}]}]},
        )
        assert response.status_code == 200

    def test_a_malformed_body_is_rejected(self, webhook_client: TestClient) -> None:
        body = b"not json"
        signature = hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
        response = webhook_client.post(
            f"{API_V1_PREFIX}/webhooks/whatsapp",
            content=body,
            headers={"X-Hub-Signature-256": f"sha256={signature}"},
        )
        assert response.status_code == 400


class TestInboundProcessing:
    def test_an_inbound_message_creates_lead_and_conversation(
        self, webhook_client: TestClient
    ) -> None:
        """A customer can message without ever having been in an audience."""
        post_webhook(webhook_client, inbound("Tell me more"))
        process_all()

        async def state(session: AsyncSession) -> tuple[int, int, int]:
            leads = len((await session.execute(select(Lead))).scalars().all())
            convos = len((await session.execute(select(Conversation))).scalars().all())
            messages = len((await session.execute(select(Message))).scalars().all())
            return leads, convos, messages

        assert run_db(state) == (1, 1, 1)

    def test_the_lead_moves_to_responded(self, webhook_client: TestClient) -> None:
        post_webhook(webhook_client, inbound("Interested"))
        process_all()

        async def status(session: AsyncSession) -> LeadStatus:
            return (await session.execute(select(Lead.status))).scalar_one()

        assert run_db(status) is LeadStatus.RESPONDED

    def test_the_service_window_is_opened(self, webhook_client: TestClient) -> None:
        """`last_inbound_at` is what later phases check before a free-form reply."""
        post_webhook(webhook_client, inbound("Hello"))
        process_all()

        async def last_inbound(session: AsyncSession) -> Any:
            return (await session.execute(select(Conversation.last_inbound_at))).scalar_one()

        assert run_db(last_inbound) is not None

    def test_conversation_history_is_recorded(self, webhook_client: TestClient) -> None:
        """Phase 9/10 read this as the AI's transcript."""
        post_webhook(webhook_client, inbound("What is the price?"))
        process_all()

        async def turns(session: AsyncSession) -> list[tuple[str, str]]:
            rows = (
                await session.execute(select(ConversationMessage.role, ConversationMessage.content))
            ).all()
            return [(row[0], row[1]) for row in rows]

        assert run_db(turns) == [("user", "What is the price?")]

    def test_processing_twice_is_idempotent(self, webhook_client: TestClient) -> None:
        """§20: the task may be redelivered."""
        post_webhook(webhook_client, inbound("Hello"))
        process_all()

        async def event_ids(session: AsyncSession) -> list[str]:
            rows = (await session.execute(select(WebhookEvent.id))).scalars().all()
            return [str(r) for r in rows]

        for event_id in run_db(event_ids):
            run_db_task(event_id)

        async def messages(session: AsyncSession) -> int:
            return len((await session.execute(select(Message))).scalars().all())

        assert run_db(messages) == 1

    def test_the_event_is_marked_processed(self, webhook_client: TestClient) -> None:
        post_webhook(webhook_client, inbound("Hello"))
        process_all()

        async def status(session: AsyncSession) -> WebhookEventStatus:
            return (await session.execute(select(WebhookEvent.status))).scalar_one()

        assert run_db(status) is WebhookEventStatus.PROCESSED

    def test_a_second_message_reuses_the_conversation(self, webhook_client: TestClient) -> None:
        post_webhook(webhook_client, inbound("First", message_id="wamid.1"))
        process_all()
        post_webhook(webhook_client, inbound("Second", message_id="wamid.2"))
        process_all()

        async def counts(session: AsyncSession) -> tuple[int, int]:
            convos = len((await session.execute(select(Conversation))).scalars().all())
            messages = len((await session.execute(select(Message))).scalars().all())
            return convos, messages

        assert run_db(counts) == (1, 2)


class TestOptOut:
    def test_stop_suppresses_the_number(self, webhook_client: TestClient) -> None:
        """[D5]/[D16]: global, phone-level, and it outlives any lead row."""
        post_webhook(webhook_client, inbound("STOP"))
        process_all()

        async def suppression(session: AsyncSession) -> str | None:
            return (
                await session.execute(select(WhatsAppSuppression.phone_e164))
            ).scalar_one_or_none()

        assert run_db(suppression) == CUSTOMER

    def test_the_lead_is_marked_opted_out(self, webhook_client: TestClient) -> None:
        post_webhook(webhook_client, inbound("stop"))
        process_all()

        async def lead(session: AsyncSession) -> tuple[LeadStatus, bool]:
            row = (await session.execute(select(Lead.status, Lead.opted_out))).one()
            return row[0], row[1]

        status, opted_out = run_db(lead)
        assert status is LeadStatus.OPTED_OUT
        assert opted_out is True

    def test_an_ordinary_message_does_not_opt_out(self, webhook_client: TestClient) -> None:
        post_webhook(webhook_client, inbound("please don't stop sending these"))
        process_all()

        async def suppressions(session: AsyncSession) -> int:
            return len((await session.execute(select(WhatsAppSuppression))).scalars().all())

        assert run_db(suppressions) == 0

    def test_queued_sends_are_cancelled(self, webhook_client: TestClient) -> None:
        """[D16]: a broadcast already prepared must not message someone who just
        said stop."""

        async def seed(session: AsyncSession) -> None:
            from app.persistence.models import Audience, WhatsAppTemplate

            audience = Audience(name="A")
            template = WhatsAppTemplate(
                name="t",
                language="en",
                provider_template_id="tpl-x",
                provider_status="APPROVED",
                category="MARKETING",
            )
            lead = Lead(phone_e164=CUSTOMER)
            session.add_all([audience, template, lead])
            await session.flush()

            campaign = Campaign(name="C", audience_id=audience.id, template_id=template.id)
            session.add(campaign)
            await session.flush()
            session.add(
                CampaignRecipient(
                    campaign_id=campaign.id,
                    lead_id=lead.id,
                    phone_e164=CUSTOMER,
                    status=RecipientStatus.QUEUED,
                )
            )

        run_db(seed)

        post_webhook(webhook_client, inbound("STOP"))
        process_all()

        async def recipient_status(session: AsyncSession) -> RecipientStatus:
            return (await session.execute(select(CampaignRecipient.status))).scalar_one()

        assert run_db(recipient_status) is RecipientStatus.SKIPPED


class TestStatusUpdates:
    def _seed_outbound(self, provider_message_id: str = "wamid.out") -> None:
        async def seed(session: AsyncSession) -> None:
            from app.domain.enums import MessageDirection

            lead = Lead(phone_e164=CUSTOMER)
            session.add(lead)
            await session.flush()
            session.add(
                Message(
                    lead_id=lead.id,
                    direction=MessageDirection.OUTBOUND,
                    status=MessageStatus.SENT,
                    provider_message_id=provider_message_id,
                    phone_e164=CUSTOMER,
                    body="Hello",
                )
            )

        run_db(seed)

    def test_delivery_updates_the_message(self, webhook_client: TestClient) -> None:
        self._seed_outbound()
        post_webhook(webhook_client, status_update("wamid.out", "delivered"))
        process_all()

        async def message(session: AsyncSession) -> tuple[MessageStatus, Any]:
            row = (await session.execute(select(Message.status, Message.delivered_at))).one()
            return row[0], row[1]

        status, delivered_at = run_db(message)
        assert status is MessageStatus.DELIVERED
        assert delivered_at is not None

    def test_read_supersedes_delivered(self, webhook_client: TestClient) -> None:
        self._seed_outbound()
        post_webhook(webhook_client, status_update("wamid.out", "delivered"))
        process_all()
        post_webhook(webhook_client, status_update("wamid.out", "read"))
        process_all()

        async def status(session: AsyncSession) -> MessageStatus:
            return (await session.execute(select(Message.status))).scalar_one()

        assert run_db(status) is MessageStatus.READ

    def test_an_out_of_order_status_does_not_regress(self, webhook_client: TestClient) -> None:
        """Webhooks arrive out of order often enough to matter; a late `delivered`
        must not walk a `read` message backwards."""
        self._seed_outbound()
        post_webhook(webhook_client, status_update("wamid.out", "read"))
        process_all()
        post_webhook(webhook_client, status_update("wamid.out", "delivered"))
        process_all()

        async def status(session: AsyncSession) -> MessageStatus:
            return (await session.execute(select(Message.status))).scalar_one()

        assert run_db(status) is MessageStatus.READ

    def test_a_failure_records_its_reason(self, webhook_client: TestClient) -> None:
        self._seed_outbound()
        payload = status_update("wamid.out", "failed")
        payload["entry"][0]["changes"][0]["value"]["statuses"][0]["errors"] = [
            {"code": 131026, "title": "Message undeliverable"}
        ]
        post_webhook(webhook_client, payload)
        process_all()

        async def message(session: AsyncSession) -> tuple[MessageStatus, str | None]:
            row = (await session.execute(select(Message.status, Message.error_message))).one()
            return row[0], row[1]

        status, error = run_db(message)
        assert status is MessageStatus.FAILED
        assert error == "Message undeliverable"

    def test_a_status_for_an_unknown_message_is_harmless(self, webhook_client: TestClient) -> None:
        """Meta also reports statuses for messages sent outside this platform."""
        post_webhook(webhook_client, status_update("wamid.unknown", "delivered"))
        process_all()

        async def status(session: AsyncSession) -> WebhookEventStatus:
            return (await session.execute(select(WebhookEvent.status))).scalar_one()

        assert run_db(status) is WebhookEventStatus.PROCESSED
