"""Scheduled campaign tests: the schedule routes, and starting at the moment.

What must hold, whatever order things happen in:

- scheduling sends nothing, and refuses what sending would refuse;
- at the moment, the campaign starts exactly once, whether the delayed task, the sweep,
  or both (even concurrently) arrive;
- a withdrawn, moved, cancelled or already-started schedule is left alone;
- a campaign that cannot start goes back to READY and the team is told, rather than
  silently never sending;
- a transient failure leaves it SCHEDULED, so the next trigger starts it.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import RetryableProviderError
from app.domain.enums import CampaignStatus, NotificationType, RecipientStatus, TemplateCategory
from app.integrations.queue import TaskRequest, TaskType
from app.persistence.models import (
    Audience,
    Campaign,
    CampaignActivity,
    CampaignRecipient,
    Lead,
    Notification,
    WhatsAppSuppression,
    WhatsAppTemplate,
)
from app.worker.handlers import campaign_schedule
from app.worker.handlers.campaign_schedule import (
    ENTITY_CAMPAIGN,
    start_if_due,
    start_scheduled_campaign,
    sweep_scheduled_campaigns,
)
from tests.integration.conftest import login
from tests.integration.helpers import (
    ensure_product_id,
    get_test_database_url,
    run_db,
    scoped_prefix,
)
from tests.integration.test_campaigns_api import (
    PHONES,
    create_campaign,
    seed_audience,
    seed_template,
)

pytestmark = pytest.mark.integration


async def _clear(session: AsyncSession) -> None:
    await session.execute(
        delete(Notification).where(Notification.type == NotificationType.CAMPAIGN_SCHEDULE_FAILED)
    )
    await session.execute(delete(CampaignActivity))
    await session.execute(delete(CampaignRecipient))
    await session.execute(delete(Campaign))
    await session.execute(delete(WhatsAppSuppression))
    await session.execute(delete(Lead))
    await session.execute(delete(Audience))
    await session.execute(delete(WhatsAppTemplate))


@pytest.fixture(autouse=True)
def _clean(seeded_users: dict[str, str]) -> Iterator[None]:
    run_db(_clear)
    yield
    run_db(_clear)
    from app.api.deps import reset_queue_provider

    reset_queue_provider()


def in_minutes(minutes: float) -> str:
    return (datetime.now(UTC) + timedelta(minutes=minutes)).isoformat()


def api_queue() -> Any:
    from app.api.deps import get_queue_provider
    from app.core.config import Settings

    return get_queue_provider(Settings())


def campaign_row(campaign_id: str) -> Campaign:
    async def fetch(session: AsyncSession) -> Campaign:
        return (
            await session.execute(select(Campaign).where(Campaign.id == uuid.UUID(campaign_id)))
        ).scalar_one()

    return run_db(fetch)


def recipient_statuses(campaign_id: str) -> set[RecipientStatus]:
    async def fetch(session: AsyncSession) -> set[RecipientStatus]:
        rows = await session.execute(
            select(CampaignRecipient.status).where(
                CampaignRecipient.campaign_id == uuid.UUID(campaign_id)
            )
        )
        return set(rows.scalars().all())

    return run_db(fetch)


def activity_types(campaign_id: str) -> list[str]:
    async def fetch(session: AsyncSession) -> list[str]:
        rows = await session.execute(
            select(CampaignActivity.event_type)
            .where(CampaignActivity.campaign_id == uuid.UUID(campaign_id))
            .order_by(CampaignActivity.created_at)
        )
        return list(rows.scalars().all())

    return run_db(fetch)


def schedule_bells(campaign_id: str) -> list[Notification]:
    async def fetch(session: AsyncSession) -> list[Notification]:
        rows = await session.execute(
            select(Notification).where(
                Notification.type == NotificationType.CAMPAIGN_SCHEDULE_FAILED,
                Notification.related_entity_id == uuid.UUID(campaign_id),
            )
        )
        return list(rows.scalars().all())

    return run_db(fetch)


def set_template_status(provider_status: str) -> None:
    async def change(session: AsyncSession) -> None:
        await session.execute(update(WhatsAppTemplate).values(provider_status=provider_status))

    run_db(change)


# ---------------------------------------------------------------------------- routes


class TestScheduleRoute:
    def _ready_campaign(self, client: TestClient) -> str:
        login(client)
        audience_id = seed_audience(client, PHONES)
        return str(create_campaign(client, audience_id, seed_template()).json()["campaign"]["id"])

    def _schedule(self, client: TestClient, campaign_id: str, when: str) -> Any:
        return client.post(
            f"{scoped_prefix()}/campaigns/{campaign_id}/schedule", json={"scheduled_for": when}
        )

    def test_scheduling_moves_the_campaign_to_scheduled(self, client: TestClient) -> None:
        campaign_id = self._ready_campaign(client)
        when = in_minutes(60)

        response = self._schedule(client, campaign_id, when)

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "SCHEDULED"
        assert datetime.fromisoformat(body["scheduled_for"]) == datetime.fromisoformat(when)
        assert "SCHEDULED" in activity_types(campaign_id)

    def test_scheduling_sends_nothing(self, client: TestClient) -> None:
        """Only the start trigger is queued. No message task, and every recipient is
        still PENDING."""
        campaign_id = self._ready_campaign(client)
        self._schedule(client, campaign_id, in_minutes(60))

        queued = api_queue().enqueued
        assert [task.task_type for task in queued] == [TaskType.START_SCHEDULED_CAMPAIGN]
        assert recipient_statuses(campaign_id) == {RecipientStatus.PENDING}

    def test_the_start_task_is_timed_for_the_moment(self, client: TestClient) -> None:
        campaign_id = self._ready_campaign(client)
        when = in_minutes(90)
        self._schedule(client, campaign_id, when)

        task = api_queue().enqueued[0]
        assert task.scheduled_for == datetime.fromisoformat(when)
        assert task.payload == {"campaign_id": campaign_id}

    def test_an_ist_time_is_stored_as_the_same_instant(self, client: TestClient) -> None:
        campaign_id = self._ready_campaign(client)
        ist = timezone(timedelta(hours=5, minutes=30))
        when = (datetime.now(ist) + timedelta(hours=2)).replace(microsecond=0)

        self._schedule(client, campaign_id, when.isoformat())

        assert campaign_row(campaign_id).scheduled_for == when

    def test_a_time_in_the_past_is_refused(self, client: TestClient) -> None:
        campaign_id = self._ready_campaign(client)
        response = self._schedule(client, campaign_id, in_minutes(-5))
        assert response.status_code == 422
        assert campaign_row(campaign_id).status is CampaignStatus.READY

    def test_a_time_without_a_timezone_is_refused(self, client: TestClient) -> None:
        campaign_id = self._ready_campaign(client)
        naive = (datetime.now(UTC) + timedelta(hours=1)).replace(tzinfo=None).isoformat()
        response = self._schedule(client, campaign_id, naive)
        assert response.status_code == 422

    def test_a_salesperson_cannot_schedule(self, client: TestClient) -> None:
        campaign_id = self._ready_campaign(client)
        login(client, "sales@example.com")
        response = self._schedule(client, campaign_id, in_minutes(60))
        assert response.status_code == 403
        assert campaign_row(campaign_id).status is CampaignStatus.READY

    def test_a_template_no_longer_approved_is_refused_now(self, client: TestClient) -> None:
        """Named while someone is looking, not discovered at the moment."""
        campaign_id = self._ready_campaign(client)
        set_template_status("PAUSED")

        response = self._schedule(client, campaign_id, in_minutes(60))

        assert response.status_code == 409
        assert "PAUSED" in response.json()["detail"]
        assert campaign_row(campaign_id).status is CampaignStatus.READY

    def test_scheduling_again_moves_the_moment(self, client: TestClient) -> None:
        campaign_id = self._ready_campaign(client)
        self._schedule(client, campaign_id, in_minutes(60))
        later = in_minutes(180)

        response = self._schedule(client, campaign_id, later)

        assert response.status_code == 200
        assert datetime.fromisoformat(response.json()["scheduled_for"]) == datetime.fromisoformat(
            later
        )
        tasks = api_queue().enqueued
        assert len(tasks) == 2
        assert tasks[0].task_id != tasks[1].task_id
        assert "RESCHEDULED" in activity_types(campaign_id)

    def test_a_queue_failure_still_saves_the_schedule(self, client: TestClient) -> None:
        """The minute sweep starts it regardless, so the person is not told it failed."""
        campaign_id = self._ready_campaign(client)
        api_queue().fail_with = RetryableProviderError("down", provider="test")

        response = self._schedule(client, campaign_id, in_minutes(60))

        assert response.status_code == 200
        assert campaign_row(campaign_id).status is CampaignStatus.SCHEDULED

    def test_a_running_campaign_cannot_be_scheduled(self, client: TestClient) -> None:
        campaign_id = self._ready_campaign(client)
        client.post(f"{scoped_prefix()}/campaigns/{campaign_id}/send")

        response = self._schedule(client, campaign_id, in_minutes(60))

        assert response.status_code == 409
        assert campaign_row(campaign_id).status is CampaignStatus.PROCESSING


class TestUnscheduleAndOverrides:
    def _scheduled_campaign(self, client: TestClient) -> str:
        login(client)
        audience_id = seed_audience(client, PHONES)
        campaign_id = str(
            create_campaign(client, audience_id, seed_template()).json()["campaign"]["id"]
        )
        client.post(
            f"{scoped_prefix()}/campaigns/{campaign_id}/schedule",
            json={"scheduled_for": in_minutes(60)},
        )
        return campaign_id

    def test_unscheduling_returns_to_ready(self, client: TestClient) -> None:
        campaign_id = self._scheduled_campaign(client)

        response = client.post(f"{scoped_prefix()}/campaigns/{campaign_id}/unschedule")

        assert response.status_code == 200
        row = campaign_row(campaign_id)
        assert row.status is CampaignStatus.READY
        assert row.scheduled_for is None
        assert "UNSCHEDULED" in activity_types(campaign_id)

    def test_unscheduling_twice_is_harmless(self, client: TestClient) -> None:
        campaign_id = self._scheduled_campaign(client)
        client.post(f"{scoped_prefix()}/campaigns/{campaign_id}/unschedule")
        response = client.post(f"{scoped_prefix()}/campaigns/{campaign_id}/unschedule")
        assert response.status_code == 200
        assert response.json()["status"] == "READY"

    def test_a_scheduled_campaign_can_be_sent_now(self, client: TestClient) -> None:
        campaign_id = self._scheduled_campaign(client)

        response = client.post(f"{scoped_prefix()}/campaigns/{campaign_id}/send")

        assert response.status_code == 200
        assert response.json()["queued"] == len(PHONES)
        assert campaign_row(campaign_id).status is CampaignStatus.PROCESSING

    def test_a_scheduled_campaign_can_be_cancelled(self, client: TestClient) -> None:
        campaign_id = self._scheduled_campaign(client)

        response = client.post(f"{scoped_prefix()}/campaigns/{campaign_id}/cancel")

        assert response.status_code == 200
        assert campaign_row(campaign_id).status is CampaignStatus.CANCELLED
        assert recipient_statuses(campaign_id) == {RecipientStatus.SKIPPED}

    def test_a_scheduled_campaign_cannot_be_archived(self, client: TestClient) -> None:
        campaign_id = self._scheduled_campaign(client)
        response = client.post(f"{scoped_prefix()}/campaigns/{campaign_id}/archive")
        assert response.status_code == 409


# ---------------------------------------------------------------------------- worker


class RecordingQueue:
    """Records what the start enqueues; can be told to fail like an outage."""

    def __init__(self) -> None:
        self.tasks: list[TaskRequest] = []
        self.fail = False

    async def enqueue(self, task: TaskRequest) -> None:
        if self.fail:
            raise RetryableProviderError("Cloud Tasks unavailable", provider="test")
        self.tasks.append(task)

    def send_ids(self) -> list[str]:
        return [t.task_id for t in self.tasks if t.task_type is TaskType.SEND_CAMPAIGN_MESSAGE]


@pytest.fixture
def worker_env(engine: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[RecordingQueue]:
    from app.core.config import reset_settings_cache
    from app.persistence import database

    monkeypatch.setenv("DATABASE_URL", get_test_database_url())
    reset_settings_cache()
    database._engine = None  # noqa: SLF001
    database._session_factory = None  # noqa: SLF001

    queue = RecordingQueue()
    monkeypatch.setattr(campaign_schedule, "build_queue_provider", lambda _settings: queue)
    yield queue
    database._engine = None  # noqa: SLF001
    database._session_factory = None  # noqa: SLF001
    reset_settings_cache()


def seed_scheduled(
    *,
    minutes_from_now: float,
    recipients: int = 3,
    status: CampaignStatus = CampaignStatus.SCHEDULED,
    template_status: str = "APPROVED",
) -> str:
    """A campaign as the schedule route leaves it, with `recipients` PENDING."""

    async def seed(session: AsyncSession) -> str:
        product_id = await ensure_product_id(session)
        audience = Audience(name="A", product_id=product_id)
        template = WhatsAppTemplate(
            name=f"t_{uuid.uuid4().hex[:6]}",
            language="en",
            provider_template_id=f"tpl-{uuid.uuid4()}",
            product_id=product_id,
            provider_status=template_status,
            category=TemplateCategory.MARKETING,
            variable_count=1,
        )
        session.add_all([audience, template])
        await session.flush()
        campaign = Campaign(
            name="Follow-up",
            audience_id=audience.id,
            product_id=product_id,
            template_id=template.id,
            status=status,
            scheduled_for=datetime.now(UTC) + timedelta(minutes=minutes_from_now),
        )
        session.add(campaign)
        await session.flush()
        for index in range(recipients):
            # +91 and ten digits, unique per row: a two-digit index then six random.
            phone = f"+9198{index:02d}{uuid.uuid4().int % 1_000_000:06d}"
            lead = Lead(phone_e164=phone, full_name=f"Dr {index}", product_id=product_id)
            session.add(lead)
            await session.flush()
            session.add(
                CampaignRecipient(
                    campaign_id=campaign.id,
                    lead_id=lead.id,
                    phone_e164=phone,
                    status=RecipientStatus.PENDING,
                )
            )
        await session.flush()
        return str(campaign.id)

    return run_db(seed)


def run(coro_factory: Any) -> Any:
    """Run handler code on its own loop, as the worker would, disposing the engine."""
    from app.persistence import database

    async def go() -> Any:
        database._engine = None  # noqa: SLF001
        database._session_factory = None  # noqa: SLF001
        try:
            return await coro_factory()
        finally:
            await database.dispose_engine()

    return asyncio.run(go())


def start_task(campaign_id: str) -> dict[str, Any]:
    return dict(
        run(
            lambda: start_scheduled_campaign(
                TaskRequest(
                    task_type=TaskType.START_SCHEDULED_CAMPAIGN,
                    payload={"campaign_id": campaign_id},
                )
            )
        )
    )


def sweep() -> dict[str, Any]:
    return dict(
        run(
            lambda: sweep_scheduled_campaigns(
                TaskRequest(task_type=TaskType.SWEEP_SCHEDULED_CAMPAIGNS)
            )
        )
    )


class TestStartingOnTime:
    def test_a_due_campaign_starts(self, worker_env: RecordingQueue) -> None:
        campaign_id = seed_scheduled(minutes_from_now=-0.1)

        result = start_task(campaign_id)

        assert result["result"] == "started"
        row = campaign_row(campaign_id)
        assert row.status is CampaignStatus.PROCESSING
        assert row.started_at is not None
        assert len(worker_env.send_ids()) == 3
        assert recipient_statuses(campaign_id) == {RecipientStatus.QUEUED}

    def test_an_early_task_does_nothing(self, worker_env: RecordingQueue) -> None:
        """A task left from a schedule that was moved later."""
        campaign_id = seed_scheduled(minutes_from_now=30)

        assert start_task(campaign_id)["result"] == "not_due"
        assert campaign_row(campaign_id).status is CampaignStatus.SCHEDULED
        assert worker_env.tasks == []

    @pytest.mark.parametrize(
        "status", [CampaignStatus.READY, CampaignStatus.CANCELLED, CampaignStatus.PROCESSING]
    )
    def test_a_withdrawn_or_started_campaign_is_left_alone(
        self, worker_env: RecordingQueue, status: CampaignStatus
    ) -> None:
        campaign_id = seed_scheduled(minutes_from_now=-1, status=status)

        assert start_task(campaign_id)["result"] == "not_scheduled"
        assert campaign_row(campaign_id).status is status
        assert worker_env.tasks == []

    def test_a_malformed_task_is_not_retried(self, worker_env: RecordingQueue) -> None:
        result = dict(
            run(
                lambda: start_scheduled_campaign(
                    TaskRequest(task_type=TaskType.START_SCHEDULED_CAMPAIGN, payload={})
                )
            )
        )
        assert result["result"] == "invalid_payload"


class TestStartsExactlyOnce:
    def test_the_task_then_the_sweep_sends_once(self, worker_env: RecordingQueue) -> None:
        campaign_id = seed_scheduled(minutes_from_now=-0.1)

        assert start_task(campaign_id)["result"] == "started"
        swept = sweep()

        assert swept["due"] == 0
        assert len(worker_env.send_ids()) == 3

    def test_the_sweep_then_the_task_sends_once(self, worker_env: RecordingQueue) -> None:
        campaign_id = seed_scheduled(minutes_from_now=-0.1)

        assert sweep()["started"] == 1
        assert start_task(campaign_id)["result"] == "not_scheduled"
        assert len(worker_env.send_ids()) == 3

    def test_two_triggers_at_the_same_instant_send_once(self, worker_env: RecordingQueue) -> None:
        """The row lock makes the second wait, then see PROCESSING."""
        campaign_id = seed_scheduled(minutes_from_now=-0.1)
        cid = uuid.UUID(campaign_id)

        async def both() -> list[str]:
            return list(
                await asyncio.gather(
                    start_if_due(cid, skip_locked=False), start_if_due(cid, skip_locked=False)
                )
            )

        outcomes = run(both)

        assert sorted(outcomes) == ["not_scheduled", "started"]
        assert len(worker_env.send_ids()) == 3
        assert len(set(worker_env.send_ids())) == 3


class TestSweep:
    def test_the_sweep_starts_due_campaigns_only(self, worker_env: RecordingQueue) -> None:
        due = seed_scheduled(minutes_from_now=-1)
        future = seed_scheduled(minutes_from_now=45)

        result = sweep()

        assert result["due"] == 1
        assert result["started"] == 1
        assert campaign_row(due).status is CampaignStatus.PROCESSING
        assert campaign_row(future).status is CampaignStatus.SCHEDULED

    def test_an_empty_sweep_is_quiet(self, worker_env: RecordingQueue) -> None:
        assert sweep() == {"result": "swept", "due": 0}

    def test_one_campaign_that_errors_does_not_stop_the_others(
        self, worker_env: RecordingQueue, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        first = seed_scheduled(minutes_from_now=-2)
        second = seed_scheduled(minutes_from_now=-1)
        real = campaign_schedule.start_if_due

        async def flaky(campaign_id: uuid.UUID, *, skip_locked: bool) -> str:
            if str(campaign_id) == first:
                raise RuntimeError("boom")
            return await real(campaign_id, skip_locked=skip_locked)

        monkeypatch.setattr(campaign_schedule, "start_if_due", flaky)
        result = sweep()

        assert result["errored"] == 1
        assert result["started"] == 1
        assert campaign_row(first).status is CampaignStatus.SCHEDULED
        assert campaign_row(second).status is CampaignStatus.PROCESSING


class TestHandingBack:
    def test_a_transient_queue_failure_leaves_it_scheduled(
        self, worker_env: RecordingQueue
    ) -> None:
        """Rolled back whole: the next trigger starts it, nobody is marked queued."""
        campaign_id = seed_scheduled(minutes_from_now=-0.1)
        worker_env.fail = True

        with pytest.raises(RetryableProviderError):
            start_task(campaign_id)

        assert campaign_row(campaign_id).status is CampaignStatus.SCHEDULED
        assert recipient_statuses(campaign_id) == {RecipientStatus.PENDING}

        worker_env.fail = False
        assert start_task(campaign_id)["result"] == "started"

    def test_a_template_paused_by_meta_hands_it_back(self, worker_env: RecordingQueue) -> None:
        campaign_id = seed_scheduled(minutes_from_now=-0.1, template_status="PAUSED")

        assert start_task(campaign_id)["result"] == "refused"

        row = campaign_row(campaign_id)
        assert row.status is CampaignStatus.READY
        assert row.scheduled_for is None
        assert worker_env.tasks == []
        assert recipient_statuses(campaign_id) == {RecipientStatus.PENDING}
        assert activity_types(campaign_id)[-1] == "SCHEDULE_FAILED"
        bells = schedule_bells(campaign_id)
        assert len(bells) == 2  # the admin and the salesperson
        assert all(bell.related_entity_type == ENTITY_CAMPAIGN for bell in bells)
        assert "PAUSED" in bells[0].message

    def test_nobody_left_to_message_hands_it_back(self, worker_env: RecordingQueue) -> None:
        campaign_id = seed_scheduled(minutes_from_now=-0.1, recipients=0)

        assert start_task(campaign_id)["result"] == "refused"
        assert campaign_row(campaign_id).status is CampaignStatus.READY
        assert schedule_bells(campaign_id)

    def test_too_late_is_not_sent(self, worker_env: RecordingQueue) -> None:
        """Every trigger was down for hours. A morning message is not sent at night."""
        campaign_id = seed_scheduled(minutes_from_now=-180)

        assert start_task(campaign_id)["result"] == "missed"

        assert campaign_row(campaign_id).status is CampaignStatus.READY
        assert worker_env.tasks == []
        assert recipient_statuses(campaign_id) == {RecipientStatus.PENDING}
        assert activity_types(campaign_id)[-1] == "SCHEDULE_MISSED"
        assert "not sent late" in schedule_bells(campaign_id)[0].message

    def test_late_but_inside_the_limit_still_sends(self, worker_env: RecordingQueue) -> None:
        campaign_id = seed_scheduled(minutes_from_now=-90)
        assert start_task(campaign_id)["result"] == "started"

    def test_a_second_failure_rings_again_with_the_new_reason(
        self, worker_env: RecordingQueue
    ) -> None:
        """The notification table allows one bell per campaign per user; the old one is
        replaced so a repeat failure is not swallowed."""
        campaign_id = seed_scheduled(minutes_from_now=-180)
        start_task(campaign_id)

        async def reschedule_with_paused_template(session: AsyncSession) -> None:
            await session.execute(
                update(Campaign)
                .where(Campaign.id == uuid.UUID(campaign_id))
                .values(
                    status=CampaignStatus.SCHEDULED,
                    scheduled_for=datetime.now(UTC) - timedelta(seconds=5),
                )
            )
            await session.execute(update(WhatsAppTemplate).values(provider_status="PAUSED"))

        run_db(reschedule_with_paused_template)
        assert start_task(campaign_id)["result"] == "refused"

        bells = schedule_bells(campaign_id)
        assert len(bells) == 2
        assert all("PAUSED" in bell.message for bell in bells)
