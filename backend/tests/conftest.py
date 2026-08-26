"""Shared test fixtures.

Every test runs against fake providers (CLAUDE.md), so the suite performs no network
I/O and can never send a real WhatsApp message.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, reset_settings_cache
from app.integrations.calendar import FakeCalendarProvider
from app.integrations.crm import FakeCRMProvider
from app.integrations.embedding import FakeEmbeddingProvider
from app.integrations.llm import FakeLLMProvider
from app.integrations.queue import InMemoryQueueProvider
from app.integrations.whatsapp import FakeWhatsAppProvider
from app.worker.registry import task_registry


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Neutralize ambient configuration.

    A developer's real .env must not change test outcomes, and a real credential must
    never be reachable from the suite. Provider-selection and secret variables are
    stripped so every test starts from fake providers.

    Stripping `os.environ` is not enough on its own: `Settings.model_config` names
    `.env`, and pydantic-settings reads that file directly for any field the environment
    does not supply. With a `backend/.env` present — the normal state for local
    development — tests asserting a field's *default* saw the file's value instead. CI
    has no `.env`, so the suite stayed green there while failing locally, which is the
    exact confusion this fixture exists to prevent. Detaching the file makes the two
    environments agree.
    """
    monkeypatch.setitem(Settings.model_config, "env_file", None)

    for key in list(os.environ):
        if key.upper().startswith(
            (
                "APP_",
                "AUDIENCE_",
                "CALENDAR_",
                "CRM_",
                "DATABASE_",
                "DEMO_",
                "EMBEDDING_",
                "GOOGLE_",
                "HUBSPOT_",
                "LLM_",
                "MSG_",
                "OPENROUTER_",
                "OPT_OUT_",
                "QUALIFICATION_",
                "QUEUE_",
                "SESSION_",
                "WHATSAPP_",
                "WORKER_",
            )
        ):
            monkeypatch.delenv(key, raising=False)

    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("LOG_FORMAT", "json")
    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture
def settings() -> Settings:
    return Settings()


@pytest.fixture(autouse=True)
def _clean_task_registry() -> Iterator[None]:
    """Keep handler registrations from leaking between tests."""
    task_registry.clear()
    yield
    task_registry.clear()


# ---------------------------------------------------------------- fake providers


@pytest.fixture
def fake_whatsapp() -> FakeWhatsAppProvider:
    return FakeWhatsAppProvider()


@pytest.fixture
def fake_llm() -> FakeLLMProvider:
    return FakeLLMProvider()


@pytest.fixture
def fake_embedding() -> FakeEmbeddingProvider:
    return FakeEmbeddingProvider(dimension=384)


@pytest.fixture
def fake_calendar() -> FakeCalendarProvider:
    return FakeCalendarProvider()


@pytest.fixture
def fake_crm() -> FakeCRMProvider:
    return FakeCRMProvider()


@pytest.fixture
def fake_queue() -> InMemoryQueueProvider:
    return InMemoryQueueProvider()


# ---------------------------------------------------------------- app clients


@pytest.fixture
def api_client(settings: Settings) -> Iterator[TestClient]:
    from app.api.main import create_app

    with TestClient(create_app(settings)) as client:
        yield client


@pytest.fixture
def worker_client(settings: Settings) -> Iterator[TestClient]:
    from app.worker.main import create_worker_app

    with TestClient(create_worker_app(settings)) as client:
        yield client
