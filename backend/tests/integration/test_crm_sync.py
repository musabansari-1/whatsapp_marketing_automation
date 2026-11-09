"""CRM synchronization tests (§14, §20, [D11], [D18]).

Half of these exist to prove a negative, which is what §14 actually asks for: a HubSpot
outage must not break WhatsApp, the AI, RAG, Calendar, the dashboard or core state. A
CRM that is down should cost a stale contact record and nothing else.
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
from app.domain import crm as crm_domain
from app.domain.enums import BookingStatus, LeadStatus
from app.integrations.crm import FakeCRMProvider
from app.integrations.queue import TaskRequest, TaskType
from app.persistence.models import Audience, Booking, Conversation, Lead, Message, Product
from app.worker.handlers.campaign_send import RetryableSendError
from app.worker.handlers.crm_sync import queue_crm_sync, sync_crm_contact
from tests.integration.helpers import ensure_product_id, get_test_database_url, run_db

pytestmark = pytest.mark.integration

PHONE = "+919876500600"
START = datetime(2026, 9, 2, 10, 0, tzinfo=UTC)


@pytest.fixture
def crm_env(engine: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeCRMProvider]:
    from app.core.config import reset_settings_cache
    from app.persistence import database

    monkeypatch.setenv("DATABASE_URL", get_test_database_url())
    reset_settings_cache()
    database._engine = None  # noqa: SLF001
    database._session_factory = None  # noqa: SLF001

    crm = FakeCRMProvider()
    monkeypatch.setattr("app.worker.handlers.crm_sync.build_crm_provider", lambda _settings: crm)

    async def clear(session: AsyncSession) -> None:
        await session.execute(delete(Booking))
        await session.execute(delete(Message))
        await session.execute(delete(Conversation))
        await session.execute(delete(Lead))
        await session.execute(delete(Audience))

    run_db(clear)
    yield crm
    run_db(clear)


def seed_lead(
    *,
    status: LeadStatus = LeadStatus.RESPONDED,
    opted_out: bool = False,
    facts: dict[str, Any] | None = None,
    with_audience: bool = False,
    with_booking: bool = False,
) -> str:
    async def create(session: AsyncSession) -> str:
        audience_id = None
        if with_audience:
            audience = Audience(
                name="August batch",
                status="CONFIRMED",
                product_id=await ensure_product_id(session),
            )
            session.add(audience)
            await session.flush()
            audience_id = audience.id

        lead = Lead(
            product_id=await ensure_product_id(session),
            phone_e164=PHONE,
            full_name="Asha Rao",
            email="asha@example.com",
            company="Northwind",
            status=status,
            opted_out=opted_out,
            qualification_data=facts or {},
            source_audience_id=audience_id,
        )
        session.add(lead)
        await session.flush()

        if with_booking:
            session.add(
                Booking(
                    product_id=lead.product_id,
                    lead_id=lead.id,
                    calendar_event_id=f"demo-{uuid.uuid4()}",
                    start_time=START,
                    end_time=START + timedelta(minutes=30),
                    timezone="Asia/Kolkata",
                    status=BookingStatus.CONFIRMED,
                    confirmed_at=datetime.now(UTC),
                )
            )

        await session.flush()
        return str(lead.id)

    return run_db(create)


def run_sync(lead_id: str) -> dict[str, Any]:
    from app.persistence import database

    async def run() -> dict[str, Any]:
        database._engine = None  # noqa: SLF001
        database._session_factory = None  # noqa: SLF001
        try:
            result = await sync_crm_contact(
                TaskRequest(
                    task_type=TaskType.SYNC_CRM_CONTACT,
                    task_id=f"sync-{lead_id}",
                    payload={"lead_id": lead_id},
                )
            )
            return dict(result)
        finally:
            await database.dispose_engine()

    return asyncio.run(run())


def lead_row(lead_id: str) -> Lead:
    async def fetch(session: AsyncSession) -> Lead:
        lead = await session.get(Lead, uuid.UUID(lead_id))
        assert lead is not None
        return lead

    return run_db(fetch)


class TestSynchronization:
    def test_a_lead_reaches_the_crm(self, crm_env: FakeCRMProvider) -> None:
        lead_id = seed_lead()

        result = run_sync(lead_id)

        assert result["synced"] is True
        assert len(crm_env.upserts) == 1
        assert crm_env.upserts[0].phone_e164 == PHONE

    def test_the_contact_id_is_recorded(self, crm_env: FakeCRMProvider) -> None:
        """A record and a debugging aid — the idempotency itself comes from the
        upsert searching by identity before writing."""
        lead_id = seed_lead()

        run_sync(lead_id)

        lead = lead_row(lead_id)
        assert lead.crm_contact_id
        assert lead.crm_synced_at is not None
        assert lead.crm_sync_error is None

    def test_syncing_twice_updates_one_contact(self, crm_env: FakeCRMProvider) -> None:
        """§20: the task is retryable, and a second contact for one person would be
        worse than no synchronization."""
        lead_id = seed_lead()

        run_sync(lead_id)
        run_sync(lead_id)

        assert len(crm_env.contacts_by_phone) == 1

    def test_current_state_is_sent_not_stale_state(self, crm_env: FakeCRMProvider) -> None:
        """The snapshot is read at sync time rather than carried in the task payload,
        so a delayed or retried task sends what is true now."""
        lead_id = seed_lead(status=LeadStatus.RESPONDED)

        async def advance(session: AsyncSession) -> None:
            lead = await session.get(Lead, uuid.UUID(lead_id))
            assert lead is not None
            lead.status = LeadStatus.QUALIFIED

        run_db(advance)
        run_sync(lead_id)

        assert crm_env.upserts[0].properties[crm_domain.PROP_LEAD_STATUS] == "QUALIFIED"

    def test_the_audience_becomes_the_source(self, crm_env: FakeCRMProvider) -> None:
        lead_id = seed_lead(with_audience=True)

        run_sync(lead_id)

        assert crm_env.upserts[0].properties[crm_domain.PROP_LEAD_SOURCE] == "August batch"

    def test_a_confirmed_booking_is_sent(self, crm_env: FakeCRMProvider) -> None:
        lead_id = seed_lead(with_booking=True)

        run_sync(lead_id)

        properties = crm_env.upserts[0].properties
        assert properties[crm_domain.PROP_DEMO_STATUS] == "CONFIRMED"
        assert properties[crm_domain.PROP_DEMO_AT]

    def test_a_cancelled_booking_is_not_sent(self, crm_env: FakeCRMProvider) -> None:
        """A cancelled demo in the CRM has a salesperson turning up to nothing."""
        lead_id = seed_lead(with_booking=True)

        async def cancel(session: AsyncSession) -> None:
            booking = (await session.execute(select(Booking))).scalars().one()
            booking.status = BookingStatus.CANCELLED

        run_db(cancel)
        run_sync(lead_id)

        assert crm_domain.PROP_DEMO_STATUS not in crm_env.upserts[0].properties

    def test_extracted_facts_are_sent(self, crm_env: FakeCRMProvider) -> None:
        """[D18]: informational in the CRM, never authoritative."""
        lead_id = seed_lead(facts={"team_size": 40})

        run_sync(lead_id)

        assert "team_size: 40" in crm_env.upserts[0].properties[crm_domain.PROP_QUALIFICATION]

    def test_opt_out_state_is_sent(self, crm_env: FakeCRMProvider) -> None:
        lead_id = seed_lead(opted_out=True, status=LeadStatus.OPTED_OUT)

        run_sync(lead_id)

        assert crm_env.upserts[0].properties[crm_domain.PROP_OPTED_OUT] == "true"


class TestFailuresDoNotBreakThePlatform:
    """§14: a HubSpot outage must not break WhatsApp, AI, RAG, Calendar, the dashboard
    or core PostgreSQL state."""

    def test_a_transient_failure_is_retried(self, crm_env: FakeCRMProvider) -> None:
        crm_env.fail_with = RetryableProviderError("hubspot unavailable", provider="fake")
        lead_id = seed_lead()

        with pytest.raises(RetryableSendError):
            run_sync(lead_id)

    def test_a_permanent_failure_settles_rather_than_retrying(
        self, crm_env: FakeCRMProvider
    ) -> None:
        crm_env.fail_with = NonRetryableProviderError("invalid property", provider="fake")
        lead_id = seed_lead()

        result = run_sync(lead_id)

        assert result["synced"] is False

    def test_a_failure_is_recorded_on_the_lead(self, crm_env: FakeCRMProvider) -> None:
        """§14 forbids raising it at the caller, so without a record a CRM failing for
        a week would be invisible until someone opened HubSpot."""
        crm_env.fail_with = NonRetryableProviderError("invalid property", provider="fake")
        lead_id = seed_lead()

        run_sync(lead_id)

        assert lead_row(lead_id).crm_sync_error

    def test_the_lead_itself_is_untouched_by_a_failure(self, crm_env: FakeCRMProvider) -> None:
        """Core PostgreSQL state is the platform's, not the CRM's."""
        crm_env.fail_with = NonRetryableProviderError("boom", provider="fake")
        lead_id = seed_lead(status=LeadStatus.QUALIFIED)

        run_sync(lead_id)

        lead = lead_row(lead_id)
        assert lead.status is LeadStatus.QUALIFIED
        assert lead.crm_contact_id is None

    def test_a_later_success_clears_the_recorded_failure(self, crm_env: FakeCRMProvider) -> None:
        crm_env.fail_with = NonRetryableProviderError("boom", provider="fake")
        lead_id = seed_lead()
        run_sync(lead_id)

        crm_env.fail_with = None
        run_sync(lead_id)

        lead = lead_row(lead_id)
        assert lead.crm_sync_error is None
        assert lead.crm_contact_id

    def test_queueing_never_raises_at_the_caller(
        self, crm_env: FakeCRMProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The callers are the conversation turn, the booking flow and the opt-out
        path. None of them should abandon its own work because a queue call failed."""
        from app.core.config import Settings

        class BrokenQueue:
            async def enqueue(self, task: TaskRequest) -> None:
                raise RuntimeError("queue unavailable")

        monkeypatch.setattr(
            "app.worker.handlers.crm_sync.build_queue_provider",
            lambda _settings: BrokenQueue(),
        )

        # Returns False rather than raising.
        assert asyncio.run(queue_crm_sync(Settings(), uuid.uuid4())) is False


class TestTaskValidation:
    def test_a_missing_lead_id_is_rejected(self, crm_env: FakeCRMProvider) -> None:
        with pytest.raises(ValueError, match="lead_id"):
            asyncio.run(
                sync_crm_contact(
                    TaskRequest(task_type=TaskType.SYNC_CRM_CONTACT, task_id="x", payload={})
                )
            )

    def test_an_unknown_lead_is_rejected(self, crm_env: FakeCRMProvider) -> None:
        with pytest.raises(ValueError, match="not found"):
            run_sync(str(uuid.uuid4()))


class TestDeferredWhileMoreThanOneProductExists:
    """The multi-product CRM design is deferred (Phase 25). Until it lands, a second product
    turns synchronization off entirely, because the flat `wa_*` properties would let one
    product overwrite another's state on the shared contact."""

    @staticmethod
    def _second_product(*, active: bool) -> uuid.UUID:
        async def create(session: AsyncSession) -> uuid.UUID:
            await ensure_product_id(session)
            product = Product(
                slug=f"crm-second-{uuid.uuid4().hex[:6]}", display_name="Second", is_active=active
            )
            session.add(product)
            await session.flush()
            return product.id

        return run_db(create)

    @staticmethod
    def _remove(product_id: uuid.UUID) -> None:
        async def remove(session: AsyncSession) -> None:
            await session.execute(delete(Product).where(Product.id == product_id))

        run_db(remove)

    @pytest.mark.parametrize("active", [True, False])
    def test_a_second_product_defers_sync_and_writes_nothing(
        self, crm_env: FakeCRMProvider, active: bool
    ) -> None:
        """Inactive counts too: its leads still exist and would still sync."""
        lead_id = seed_lead()
        second = self._second_product(active=active)
        try:
            result = run_sync(lead_id)
        finally:
            self._remove(second)

        assert result == {"result": "deferred", "lead_id": lead_id, "synced": False}
        assert crm_env.upserts == [], "nothing may reach the CRM"

        lead = lead_row(lead_id)
        assert lead.crm_contact_id is None
        assert lead.crm_synced_at is None
        assert lead.crm_sync_error is None, "a deferral is not a failure"

    def test_a_single_product_still_syncs(self, crm_env: FakeCRMProvider) -> None:
        """The existing single-product integration keeps working."""
        lead_id = seed_lead()

        assert run_sync(lead_id)["synced"] is True
        assert len(crm_env.upserts) == 1


class _RecordingMIS:
    """Stands in for `MISClient`; answers like the MIS does."""

    calls: list[dict[str, Any]] = []
    answer: dict[str, Any] = {"ok": True, "lead_id": "mis-1", "created": True}
    error: Exception | None = None

    def __init__(self, **_kwargs: Any) -> None:
        pass

    async def post_event(
        self, *, workspace: str, secret: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        if _RecordingMIS.error is not None:
            raise _RecordingMIS.error
        _RecordingMIS.calls.append({"workspace": workspace, "secret": secret, "data": data})
        return _RecordingMIS.answer


@pytest.fixture
def mis_env(crm_env: FakeCRMProvider, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """CRM_PROVIDER=mis with the test product mapped to an MIS workspace."""
    from app.core.config import reset_settings_cache

    async def slug(session: AsyncSession) -> str:
        product = await session.get(Product, await ensure_product_id(session))
        assert product is not None
        return product.slug

    product_slug = run_db(slug)
    monkeypatch.setenv("CRM_PROVIDER", "mis")
    monkeypatch.setenv("MIS_EVENTS_URL", "https://mis.example/api/wa-event")
    monkeypatch.setenv("MIS_WORKSPACES", f'{{"{product_slug}": "lifeherba"}}')
    monkeypatch.setenv("MIS_SECRETS", f'{{"{product_slug}": "s3"}}')
    reset_settings_cache()
    _RecordingMIS.calls = []
    _RecordingMIS.answer = {"ok": True, "lead_id": "mis-1", "created": True}
    _RecordingMIS.error = None
    monkeypatch.setattr("app.worker.handlers.crm_sync.MISClient", _RecordingMIS)
    yield product_slug
    reset_settings_cache()


class TestMISSync:
    def test_an_engaged_lead_reaches_its_workspace(self, mis_env: str) -> None:
        lead_id = seed_lead(status=LeadStatus.INTERESTED)

        result = run_sync(lead_id)

        assert result["synced"] is True
        [call] = _RecordingMIS.calls
        assert call["workspace"] == "lifeherba"
        assert call["secret"] == "s3"
        assert call["data"]["ref"] == lead_id
        assert call["data"]["status"] == "INTERESTED"
        row = lead_row(lead_id)
        assert row.crm_contact_id == "mis-1"
        assert row.crm_synced_at is not None

    def test_is_not_deferred_with_several_products(self, mis_env: str) -> None:
        """One workspace per product, so the flat-property deferral does not apply."""

        async def add_product(session: AsyncSession) -> None:
            await ensure_product_id(session)
            session.add(Product(slug=f"other-{uuid.uuid4().hex[:6]}", display_name="Other"))
            await session.flush()

        run_db(add_product)
        lead_id = seed_lead()

        assert run_sync(lead_id)["synced"] is True

    def test_a_lead_that_never_replied_is_not_created(self, mis_env: str) -> None:
        lead_id = seed_lead(status=LeadStatus.CONTACTED)

        assert run_sync(lead_id)["result"] == "skipped"
        assert _RecordingMIS.calls == []

    def test_an_unmapped_product_is_skipped(
        self, mis_env: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.core.config import reset_settings_cache

        monkeypatch.setenv("MIS_WORKSPACES", "{}")
        reset_settings_cache()
        lead_id = seed_lead()

        assert run_sync(lead_id)["result"] == "skipped"
        assert _RecordingMIS.calls == []

    def test_a_permanent_failure_is_recorded_not_raised(self, mis_env: str) -> None:
        _RecordingMIS.error = NonRetryableProviderError("[401] Unauthorised", provider="mis")
        lead_id = seed_lead()

        assert run_sync(lead_id)["result"] == "failed"
        assert "Unauthorised" in (lead_row(lead_id).crm_sync_error or "")

    def test_an_outage_is_retried(self, mis_env: str) -> None:
        _RecordingMIS.error = RetryableProviderError("down", provider="mis")
        lead_id = seed_lead()

        with pytest.raises(RetryableSendError):
            run_sync(lead_id)
