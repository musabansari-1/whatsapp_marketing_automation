"""CRM synchronization is deferred while more than one product exists (Phase 26).

The existing CRM properties are flat — `wa_lead_status`, `wa_campaign`, `wa_demo_at` and
the rest carry no product. One HubSpot contact is one human ([D44]) who may be a separate
lead in several products ([D41]), so writing them from more than one product would let
the last sync overwrite every other product's state. The multi-product CRM design is
deferred; this guard is what keeps the deferral from corrupting anything meanwhile.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.config import Settings
from app.domain.crm import single_product_sync_is_safe
from app.integrations.queue import EnqueuedTask, TaskRequest, TaskType
from app.worker.handlers.crm_sync import queue_crm_sync


class TestDeferralGuard:
    @pytest.mark.parametrize("count", [0, 1])
    def test_single_product_sync_stays_available(self, count: int) -> None:
        """A single-product deployment keeps the integration it already had."""
        assert single_product_sync_is_safe(product_count=count) is True

    @pytest.mark.parametrize("count", [2, 3, 10])
    def test_more_than_one_product_defers_sync(self, count: int) -> None:
        assert single_product_sync_is_safe(product_count=count) is False


class TestSyncKillSwitch:
    """`CRM_SYNC_ENABLED=false` skips the enqueue entirely, so a deferred
    deployment stops paying queue round-trips for tasks the worker would only
    refuse. The task's own deferral check stays as backstop."""

    @pytest.mark.asyncio
    async def test_disabled_sync_enqueues_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CRM_SYNC_ENABLED", "false")
        settings = Settings()

        enqueued: list[TaskRequest] = []

        class RecordingQueue:
            async def enqueue(self, task: TaskRequest) -> EnqueuedTask:
                enqueued.append(task)
                return EnqueuedTask(task_id=task.task_id, task_type=task.task_type)

        monkeypatch.setattr(
            "app.worker.handlers.crm_sync.build_queue_provider",
            lambda settings: RecordingQueue(),
        )

        assert await queue_crm_sync(settings, uuid.uuid4()) is False
        assert enqueued == []

    @pytest.mark.asyncio
    async def test_enabled_sync_enqueues_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Default on: current behavior is unchanged unless an operator acts."""
        settings = Settings()

        enqueued: list[TaskRequest] = []

        class RecordingQueue:
            async def enqueue(self, task: TaskRequest) -> EnqueuedTask:
                enqueued.append(task)
                return EnqueuedTask(task_id=task.task_id, task_type=task.task_type)

        monkeypatch.setattr(
            "app.worker.handlers.crm_sync.build_queue_provider",
            lambda settings: RecordingQueue(),
        )

        assert await queue_crm_sync(settings, uuid.uuid4()) is True
        assert len(enqueued) == 1
        assert enqueued[0].task_type is TaskType.SYNC_CRM_CONTACT
