"""Campaign send handler tests (§7, §18, §20, [D16], [D27], [D29]).

Covers the eleven scenarios the phase requires: successful delivery, transient failure
retried to success, transient failure exhausting attempts, permanent failure, rate
limiting, duplicate task delivery, a cancelled campaign with queued tasks, the
cancellation race, worker restart, concurrent workers, and idempotent processing.

Sending goes through `FakeWhatsAppProvider`, so no real message can leave.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NonRetryableProviderError, RetryableProviderError
from app.domain.enums import (
    CampaignStatus,
    MessageStatus,
    RecipientStatus,
    TemplateCategory,
)
from app.integrations.queue import TaskRequest, TaskType
from app.integrations.whatsapp import FakeWhatsAppProvider
from app.persistence.models import (
    Audience,
    Campaign,
    CampaignRecipient,
    Lead,
    Message,
    WhatsAppSuppression,
    WhatsAppTemplate,
)
from app.worker.handlers.campaign_send import RetryableSendError, send_campaign_message
from tests.integration.helpers import ensure_product_id, get_test_database_url, run_db

pytestmark = pytest.mark.integration

PHONE = "+919876543210"


@pytest.fixture
def send_env(engine: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeWhatsAppProvider]:
    """A clean database and a fake provider the handler will use.

    `build_whatsapp_provider` is patched to return one shared instance, so a test can
    inspect what was sent and inject failures.
    """
    from app.persistence import database

    monkeypatch.setenv("DATABASE_URL", get_test_database_url())
    from app.core.config import reset_settings_cache

    reset_settings_cache()
    database._engine = None  # noqa: SLF001
    database._session_factory = None  # noqa: SLF001

    provider = FakeWhatsAppProvider()
    monkeypatch.setattr(
        "app.worker.handlers.campaign_send.build_whatsapp_provider",
        lambda _settings, **_: provider,
    )

    async def clear(session: AsyncSession) -> None:
        await session.execute(delete(Message))
        await session.execute(delete(CampaignRecipient))
        await session.execute(delete(Campaign))
        await session.execute(delete(WhatsAppSuppression))
        await session.execute(delete(Lead))
        await session.execute(delete(Audience))
        await session.execute(delete(WhatsAppTemplate))

    run_db(clear)
    yield provider
    run_db(clear)
    database._engine = None  # noqa: SLF001
    database._session_factory = None  # noqa: SLF001
    reset_settings_cache()


def seed_campaign(
    *,
    status: CampaignStatus = CampaignStatus.PROCESSING,
    recipient_status: RecipientStatus = RecipientStatus.QUEUED,
    phone: str = PHONE,
    variable_count: int = 1,
) -> str:
    """Create a campaign with one recipient, returning the recipient id."""

    async def seed(session: AsyncSession) -> str:
        audience = Audience(name="A", product_id=await ensure_product_id(session))
        template = WhatsAppTemplate(
            name="q3_promo",
            language="en",
            provider_template_id=f"tpl-{uuid.uuid4()}",
            # Owned by the campaign's product, as [D57] requires of any template in use.
            product_id=audience.product_id,
            provider_status="APPROVED",
            category=TemplateCategory.MARKETING,
            variable_count=variable_count,
        )
        lead = Lead(phone_e164=phone, full_name="Rahul", product_id=audience.product_id)
        session.add_all([audience, template, lead])
        await session.flush()

        campaign = Campaign(
            name="C",
            audience_id=audience.id,
            # Derived from the audience, as the repository does (§28.6).
            product_id=audience.product_id,
            template_id=template.id,
            status=status,
        )
        session.add(campaign)
        await session.flush()

        recipient = CampaignRecipient(
            campaign_id=campaign.id,
            lead_id=lead.id,
            phone_e164=phone,
            status=recipient_status,
        )
        session.add(recipient)
        await session.flush()
        return str(recipient.id)

    return run_db(seed)


def run_handler(recipient_id: str) -> dict[str, Any]:
    """Run the handler on its own event loop, as the worker would.

    The singletons are reset so the handler builds its engine on this loop; asyncpg
    connections are bound to the loop that created them.
    """
    from app.persistence import database

    async def run() -> dict[str, Any]:
        database._engine = None  # noqa: SLF001
        database._session_factory = None  # noqa: SLF001
        try:
            result = await send_campaign_message(
                TaskRequest(
                    task_type=TaskType.SEND_CAMPAIGN_MESSAGE,
                    task_id=f"send-{recipient_id}",
                    payload={"recipient_id": recipient_id},
                )
            )
            return dict(result)
        finally:
            await database.dispose_engine()

    return asyncio.run(run())


def recipient_row(recipient_id: str) -> CampaignRecipient:
    async def fetch(session: AsyncSession) -> CampaignRecipient:
        return (
            await session.execute(
                select(CampaignRecipient).where(CampaignRecipient.id == uuid.UUID(recipient_id))
            )
        ).scalar_one()

    return run_db(fetch)


def message_count() -> int:
    async def count(session: AsyncSession) -> int:
        return len((await session.execute(select(Message))).scalars().all())

    return run_db(count)


class TestSuccessfulDelivery:
    def test_a_message_is_sent_and_recorded(self, send_env: FakeWhatsAppProvider) -> None:
        recipient_id = seed_campaign()
        result = run_handler(recipient_id)

        assert result["result"] == "sent"
        assert send_env.total_sent == 1
        assert send_env.recipients() == [PHONE]

    def test_the_recipient_moves_to_sent(self, send_env: FakeWhatsAppProvider) -> None:
        recipient_id = seed_campaign()
        run_handler(recipient_id)

        recipient = recipient_row(recipient_id)
        assert recipient.status is RecipientStatus.SENT
        assert recipient.provider_message_id
        assert recipient.sent_at is not None
        assert recipient.attempts == 1

    def test_an_outbound_message_row_is_created(self, send_env: FakeWhatsAppProvider) -> None:
        """Status webhooks correlate on the provider message ID, so the row must exist
        before those arrive (§8)."""
        recipient_id = seed_campaign()
        run_handler(recipient_id)

        async def message(session: AsyncSession) -> Message:
            return (await session.execute(select(Message))).scalar_one()

        stored = run_db(message)
        assert stored.status is MessageStatus.SENT
        assert stored.provider_message_id
        assert stored.idempotency_key == f"send-{recipient_id}"

    def test_the_contact_name_fills_the_first_variable(
        self, send_env: FakeWhatsAppProvider
    ) -> None:
        recipient_id = seed_campaign()
        run_handler(recipient_id)
        assert send_env.sent_templates[0].variables[0] == "Rahul"

    def test_the_campaign_completes_when_nothing_is_outstanding(
        self, send_env: FakeWhatsAppProvider
    ) -> None:
        recipient_id = seed_campaign()
        run_handler(recipient_id)

        async def campaign(session: AsyncSession) -> Campaign:
            return (await session.execute(select(Campaign))).scalar_one()

        finished = run_db(campaign)
        assert finished.status is CampaignStatus.COMPLETED
        assert finished.sent_count == 1
        assert finished.completed_at is not None

    def test_the_campaign_completes_when_its_last_recipient_fails(
        self, send_env: FakeWhatsAppProvider
    ) -> None:
        """D-03: completion used to be evaluated only after a *successful* send, so a
        campaign whose final recipient failed stayed PROCESSING forever. The recipient
        states were right and the campaign's own state was not."""
        recipient_id = seed_campaign()
        send_env.fail_permanently()

        result = run_handler(recipient_id)
        assert result["result"] == "failed_permanent"

        async def campaign(session: AsyncSession) -> Campaign:
            return (await session.execute(select(Campaign))).scalar_one()

        finished = run_db(campaign)
        assert finished.status is CampaignStatus.COMPLETED
        assert finished.failed_count == 1
        assert finished.completed_at is not None

    def test_a_campaign_stays_processing_while_a_retry_is_due(
        self, send_env: FakeWhatsAppProvider
    ) -> None:
        """The other half of D-03: a retryable failure leaves the recipient QUEUED, so
        the campaign still has work outstanding and must not be closed."""
        recipient_id = seed_campaign()
        send_env.fail_retryably()

        with pytest.raises(RetryableSendError):
            run_handler(recipient_id)

        async def campaign(session: AsyncSession) -> Campaign:
            return (await session.execute(select(Campaign))).scalar_one()

        assert run_db(campaign).status is CampaignStatus.PROCESSING


class TestTransientFailure:
    def test_a_transient_failure_asks_for_a_retry(self, send_env: FakeWhatsAppProvider) -> None:
        recipient_id = seed_campaign()
        send_env.fail_retryably()

        with pytest.raises(RetryableSendError) as exc_info:
            run_handler(recipient_id)

        # [D27]: the caller is told how long to wait.
        assert exc_info.value.retry_after_seconds > 0

    def test_the_attempt_is_counted_and_the_recipient_stays_unsent(
        self, send_env: FakeWhatsAppProvider
    ) -> None:
        """Left QUEUED rather than FAILED: attempts remain, so the retry must pick it
        up."""
        recipient_id = seed_campaign()
        send_env.fail_retryably()

        with pytest.raises(RetryableSendError):
            run_handler(recipient_id)

        recipient = recipient_row(recipient_id)
        assert recipient.attempts == 1
        assert recipient.status is RecipientStatus.QUEUED

    def test_transient_failure_then_success(self, send_env: FakeWhatsAppProvider) -> None:
        """The scenario retries exist for."""
        recipient_id = seed_campaign()

        send_env.fail_retryably()
        with pytest.raises(RetryableSendError):
            run_handler(recipient_id)

        send_env.stop_failing()
        result = run_handler(recipient_id)

        assert result["result"] == "sent"
        assert recipient_row(recipient_id).status is RecipientStatus.SENT
        assert recipient_row(recipient_id).attempts == 2
        assert send_env.total_sent == 1

    def test_exhausting_attempts_fails_the_recipient(self, send_env: FakeWhatsAppProvider) -> None:
        """[D27]: five attempts, then stop."""
        recipient_id = seed_campaign()
        send_env.fail_retryably()

        for _ in range(4):
            with pytest.raises(RetryableSendError):
                run_handler(recipient_id)

        result = run_handler(recipient_id)

        assert result["result"] == "failed_exhausted"
        recipient = recipient_row(recipient_id)
        assert recipient.status is RecipientStatus.FAILED
        assert recipient.attempts == 5
        assert recipient.error_message

    def test_rate_limiting_is_retried(self, send_env: FakeWhatsAppProvider) -> None:
        recipient_id = seed_campaign()
        send_env.fail_with = RetryableProviderError("Rate limited: slow down", provider="fake")

        with pytest.raises(RetryableSendError):
            run_handler(recipient_id)

        assert recipient_row(recipient_id).status is RecipientStatus.QUEUED


class TestPermanentFailure:
    def test_a_permanent_failure_is_not_retried(self, send_env: FakeWhatsAppProvider) -> None:
        """[D27]: retrying cannot help, so the recipient fails on the first attempt."""
        recipient_id = seed_campaign()
        send_env.fail_permanently()

        result = run_handler(recipient_id)

        assert result["result"] == "failed_permanent"
        recipient = recipient_row(recipient_id)
        assert recipient.status is RecipientStatus.FAILED
        assert recipient.attempts == 1
        assert recipient.failed_at is not None

    def test_a_failed_campaign_still_completes(self, send_env: FakeWhatsAppProvider) -> None:
        """A campaign whose recipients failed is finished, not stuck. There is no
        PARTIAL state; the counts carry the breakdown.

        This assertion changed in Phase 14. It previously asserted PROCESSING and
        described that as "still PROCESSING until the next pass" — but there was no next
        pass, which was D-03: completion was only ever evaluated after a *successful*
        send, so a campaign whose last recipient failed never closed. The test name was
        right and the assertion was wrong.
        """
        recipient_id = seed_campaign()
        send_env.fail_permanently()
        run_handler(recipient_id)

        async def campaign(session: AsyncSession) -> Campaign:
            return (await session.execute(select(Campaign))).scalar_one()

        assert recipient_row(recipient_id).status is RecipientStatus.FAILED

        finished = run_db(campaign)
        assert finished.status is CampaignStatus.COMPLETED
        assert finished.failed_count == 1


class TestIdempotency:
    def test_a_duplicate_task_does_not_send_twice(self, send_env: FakeWhatsAppProvider) -> None:
        """§20: Cloud Tasks redelivers."""
        recipient_id = seed_campaign()

        first = run_handler(recipient_id)
        second = run_handler(recipient_id)

        assert first["result"] == "sent"
        assert second["result"] == "already_settled"
        assert send_env.total_sent == 1
        assert message_count() == 1

    def test_an_already_sent_recipient_is_left_alone(self, send_env: FakeWhatsAppProvider) -> None:
        recipient_id = seed_campaign(recipient_status=RecipientStatus.SENT)
        result = run_handler(recipient_id)

        assert result["result"] == "already_settled"
        assert send_env.total_sent == 0

    def test_a_skipped_recipient_is_not_resurrected(self, send_env: FakeWhatsAppProvider) -> None:
        recipient_id = seed_campaign(recipient_status=RecipientStatus.SKIPPED)
        run_handler(recipient_id)
        assert send_env.total_sent == 0

    def test_a_crash_after_sending_is_recovered_not_repeated(
        self, send_env: FakeWhatsAppProvider
    ) -> None:
        """Worker restart: the message row proves the send happened, so the retry
        adopts it instead of sending again."""
        recipient_id = seed_campaign()

        async def simulate_crash(session: AsyncSession) -> None:
            recipient = (
                await session.execute(
                    select(CampaignRecipient).where(CampaignRecipient.id == uuid.UUID(recipient_id))
                )
            ).scalar_one()
            session.add(
                Message(
                    lead_id=recipient.lead_id,
                    campaign_id=recipient.campaign_id,
                    product_id=await ensure_product_id(session),
                    direction="OUTBOUND",
                    status=MessageStatus.SENT,
                    provider_message_id="wamid.prior",
                    phone_e164=recipient.phone_e164,
                    idempotency_key=f"send-{recipient_id}",
                )
            )

        run_db(simulate_crash)

        result = run_handler(recipient_id)

        assert result["result"] == "recovered_prior_send"
        assert send_env.total_sent == 0
        assert recipient_row(recipient_id).provider_message_id == "wamid.prior"


class TestCancellation:
    def test_a_cancelled_campaign_sends_nothing(self, send_env: FakeWhatsAppProvider) -> None:
        """[D29]: the task was queued before the cancellation, so the worker must
        re-check."""
        recipient_id = seed_campaign(status=CampaignStatus.CANCELLED)

        result = run_handler(recipient_id)

        assert result["result"] == "skipped_campaign_cancelled"
        assert send_env.total_sent == 0
        assert recipient_row(recipient_id).status is RecipientStatus.SKIPPED

    def test_cancelling_between_queueing_and_sending(self, send_env: FakeWhatsAppProvider) -> None:
        """The race [D29] describes: the campaign is cancelled after the task is
        dispatched but before it runs."""
        recipient_id = seed_campaign()

        async def cancel(session: AsyncSession) -> None:
            campaign = (await session.execute(select(Campaign))).scalar_one()
            campaign.status = CampaignStatus.CANCELLED

        run_db(cancel)

        result = run_handler(recipient_id)
        assert result["result"] == "skipped_campaign_cancelled"
        assert send_env.total_sent == 0

    def test_a_paused_campaign_defers_rather_than_skips(
        self, send_env: FakeWhatsAppProvider
    ) -> None:
        """A pause is expected to end, so the recipient stays sendable."""
        recipient_id = seed_campaign(status=CampaignStatus.PAUSED)

        result = run_handler(recipient_id)

        assert result["result"] == "deferred_campaign_paused"
        assert send_env.total_sent == 0
        assert recipient_row(recipient_id).status is RecipientStatus.QUEUED

    def test_a_completed_campaign_sends_nothing(self, send_env: FakeWhatsAppProvider) -> None:
        recipient_id = seed_campaign(status=CampaignStatus.COMPLETED)
        run_handler(recipient_id)
        assert send_env.total_sent == 0


class TestSuppressionRecheck:
    def test_a_number_suppressed_after_queueing_is_not_messaged(
        self, send_env: FakeWhatsAppProvider
    ) -> None:
        """[D16]: the customer may opt out between the broadcast and this task."""
        recipient_id = seed_campaign()

        async def suppress(session: AsyncSession) -> None:
            session.add(WhatsAppSuppression(phone_e164=PHONE))

        run_db(suppress)

        result = run_handler(recipient_id)

        assert result["result"] == "skipped_suppressed"
        assert send_env.total_sent == 0
        assert recipient_row(recipient_id).status is RecipientStatus.SKIPPED


class TestConcurrentWorkers:
    def test_two_workers_on_one_recipient_send_once(self, send_env: FakeWhatsAppProvider) -> None:
        """Cloud Tasks can deliver the same task to two instances. The row lock plus
        the status check mean only one send happens."""
        recipient_id = seed_campaign()

        results = [run_handler(recipient_id) for _ in range(3)]

        assert [r["result"] for r in results] == [
            "sent",
            "already_settled",
            "already_settled",
        ]
        assert send_env.total_sent == 1
        assert message_count() == 1


class TestFailureHandling:
    def test_a_missing_recipient_is_not_retried_forever(
        self, send_env: FakeWhatsAppProvider
    ) -> None:
        """A ValueError is not a RetryableSendError, so the HTTP layer turns it into a
        500 that Cloud Tasks eventually abandons rather than an endless 503 loop."""
        with pytest.raises(ValueError, match="not found"):
            run_handler(str(uuid.uuid4()))

    def test_a_missing_payload_is_rejected(self, send_env: FakeWhatsAppProvider) -> None:
        async def run() -> None:
            await send_campaign_message(
                TaskRequest(task_type=TaskType.SEND_CAMPAIGN_MESSAGE, payload={})
            )

        with pytest.raises(ValueError, match="recipient_id"):
            asyncio.run(run())

    def test_an_unexpected_error_is_treated_as_transient(
        self, send_env: FakeWhatsAppProvider
    ) -> None:
        """Giving up on something unrecognised risks abandoning a recoverable send."""
        recipient_id = seed_campaign()
        send_env.fail_with = RuntimeError("something odd")

        with pytest.raises(RetryableSendError):
            run_handler(recipient_id)

    def test_a_non_retryable_provider_error_stops_immediately(
        self, send_env: FakeWhatsAppProvider
    ) -> None:
        recipient_id = seed_campaign()
        send_env.fail_with = NonRetryableProviderError("invalid recipient", provider="fake")

        result = run_handler(recipient_id)
        assert result["result"] == "failed_permanent"
