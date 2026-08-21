"""Worker service and dispatch tests."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.integrations.queue import InMemoryQueueProvider, TaskRequest, TaskType
from app.worker.dispatcher import dispatch_task
from app.worker.registry import TaskRegistry, UnknownTaskTypeError, task_registry


class TestWorkerHealth:
    def test_liveness_reports_the_worker_service(self, worker_client: TestClient) -> None:
        response = worker_client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["service"] == "worker"

    def test_health_reports_the_registered_handlers(self, worker_client: TestClient) -> None:
        """Handlers arrive with the phases that own their tasks. Phase 6 added the
        WhatsApp ones; sending is still unregistered until Phase 7."""
        registered = worker_client.get("/health").json()["registered_task_types"]
        assert "process_inbound_message" in registered
        assert "sync_whatsapp_templates" in registered
        assert "send_campaign_message" not in registered


class TestTaskRegistry:
    def test_register_and_get(self) -> None:
        registry = TaskRegistry()

        async def handler(task: TaskRequest) -> str:
            return "done"

        registry.register(TaskType.SEND_CAMPAIGN_MESSAGE, handler)
        assert registry.get(TaskType.SEND_CAMPAIGN_MESSAGE) is handler
        assert registry.registered_types() == [TaskType.SEND_CAMPAIGN_MESSAGE]

    def test_unknown_type_raises(self) -> None:
        with pytest.raises(UnknownTaskTypeError):
            TaskRegistry().get(TaskType.SYNC_CRM_CONTACT)

    def test_duplicate_registration_is_rejected(self) -> None:
        registry = TaskRegistry()

        async def handler(task: TaskRequest) -> None:
            return None

        registry.register(TaskType.GENERATE_EMBEDDINGS, handler)
        with pytest.raises(ValueError, match="already registered"):
            registry.register(TaskType.GENERATE_EMBEDDINGS, handler)


class TestDispatch:
    async def test_dispatch_invokes_the_handler(self) -> None:
        received: list[TaskRequest] = []

        async def handler(task: TaskRequest) -> str:
            received.append(task)
            return "ok"

        task_registry.register(TaskType.SEND_CAMPAIGN_MESSAGE, handler)
        task = TaskRequest(task_type=TaskType.SEND_CAMPAIGN_MESSAGE, payload={"lead_id": "l-1"})

        result = await dispatch_task(task)

        assert result == "ok"
        assert received == [task]

    async def test_dispatch_raises_for_unregistered_type(self) -> None:
        with pytest.raises(UnknownTaskTypeError):
            await dispatch_task(TaskRequest(task_type=TaskType.SYNC_CRM_CONTACT))

    async def test_handler_errors_propagate(self) -> None:
        async def handler(task: TaskRequest) -> None:
            raise RuntimeError("handler blew up")

        task_registry.register(TaskType.GENERATE_EMBEDDINGS, handler)
        with pytest.raises(RuntimeError, match="handler blew up"):
            await dispatch_task(TaskRequest(task_type=TaskType.GENERATE_EMBEDDINGS))


class TestTaskEndpoint:
    def test_executes_a_registered_task(self, worker_client: TestClient) -> None:
        seen: list[str] = []

        async def handler(task: TaskRequest) -> None:
            seen.append(task.task_id)

        # A task type no phase has claimed yet, so this does not collide with the
        # handlers the worker registers at startup.
        task_registry.register(TaskType.GENERATE_EMBEDDINGS, handler)

        response = worker_client.post(
            "/internal/tasks",
            json={
                "task_type": "generate_embeddings",
                "task_id": "task-1",
                "payload": {"document_id": "d-1"},
            },
        )

        assert response.status_code == 200
        assert response.json() == {
            "status": "completed",
            "task_id": "task-1",
            "task_type": "generate_embeddings",
        }
        assert seen == ["task-1"]

    def test_unregistered_task_is_rejected_without_retry(self, worker_client: TestClient) -> None:
        """400 not 500: redelivering a task nothing handles would retry forever."""
        response = worker_client.post(
            "/internal/tasks",
            json={"task_type": "sync_crm_contact", "task_id": "task-2"},
        )
        assert response.status_code == 400

    def test_malformed_envelope_is_rejected(self, worker_client: TestClient) -> None:
        response = worker_client.post("/internal/tasks", json={"task_type": "not_a_real_task"})
        assert response.status_code == 422


class TestInlineExecution:
    async def test_inline_queue_runs_tasks_through_the_dispatcher(self) -> None:
        """Local development gets end-to-end behaviour with no Cloud Tasks emulator."""
        executed: list[TaskType] = []

        async def handler(task: TaskRequest) -> None:
            executed.append(task.task_type)

        task_registry.register(TaskType.SYNC_WHATSAPP_TEMPLATES, handler)
        queue = InMemoryQueueProvider()
        queue.set_dispatcher(dispatch_task)

        await queue.enqueue(TaskRequest(task_type=TaskType.SYNC_WHATSAPP_TEMPLATES))

        assert executed == [TaskType.SYNC_WHATSAPP_TEMPLATES]
