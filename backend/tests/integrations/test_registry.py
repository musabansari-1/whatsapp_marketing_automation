"""Provider selection tests.

The registry is the only place allowed to name a concrete provider, so this is where
the CLAUDE.md rule is enforced and where premature selection must fail loudly.
"""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.core.errors import ProviderNotImplementedError
from app.integrations.calendar import CalendarProvider, FakeCalendarProvider
from app.integrations.crm import CRMProvider, FakeCRMProvider
from app.integrations.embedding import EmbeddingProvider, FakeEmbeddingProvider
from app.integrations.llm import FakeLLMProvider, LLMProvider
from app.integrations.queue import InMemoryQueueProvider, QueueProvider
from app.integrations.registry import (
    build_calendar_provider,
    build_crm_provider,
    build_embedding_provider,
    build_llm_provider,
    build_providers,
    build_queue_provider,
    build_whatsapp_provider,
)
from app.integrations.whatsapp import FakeWhatsAppProvider, WhatsAppProvider


class TestFakeSelection:
    def test_builds_all_six_fakes(self, settings: Settings) -> None:
        providers = build_providers(settings)
        assert isinstance(providers.whatsapp, FakeWhatsAppProvider)
        assert isinstance(providers.llm, FakeLLMProvider)
        assert isinstance(providers.embedding, FakeEmbeddingProvider)
        assert isinstance(providers.calendar, FakeCalendarProvider)
        assert isinstance(providers.crm, FakeCRMProvider)
        assert isinstance(providers.queue, InMemoryQueueProvider)

    def test_fakes_satisfy_their_interfaces(self, settings: Settings) -> None:
        providers = build_providers(settings)
        assert isinstance(providers.whatsapp, WhatsAppProvider)
        assert isinstance(providers.llm, LLMProvider)
        assert isinstance(providers.embedding, EmbeddingProvider)
        assert isinstance(providers.calendar, CalendarProvider)
        assert isinstance(providers.crm, CRMProvider)
        assert isinstance(providers.queue, QueueProvider)

    def test_embedding_dimension_follows_configuration(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """[D1]: the fake must match the configured pgvector width."""
        monkeypatch.setenv("EMBEDDING_DIMENSION", "512")
        assert build_embedding_provider(Settings()).dimension == 512

    def test_the_local_embedding_provider_is_selectable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Phase 8 implemented it ([D1]). Building must not load the model: the
        registry runs in the API service too, which never embeds anything.
        """
        from app.integrations.embedding.local_sentence_transformer import (
            LocalSentenceTransformerProvider,
        )

        monkeypatch.setenv("EMBEDDING_PROVIDER", "local_sentence_transformer")
        provider = build_embedding_provider(Settings())

        assert isinstance(provider, LocalSentenceTransformerProvider)
        assert provider._model is None  # noqa: SLF001 - laziness is the point


class TestUnimplementedProvidersFailLoudly:
    @pytest.mark.parametrize(
        ("env_var", "value", "builder", "expected_phase"),
        [
            ("LLM_PROVIDER", "openrouter", "llm", "Phase 10"),
            ("CALENDAR_PROVIDER", "google", "calendar", "Phase 11"),
            ("CRM_PROVIDER", "hubspot", "crm", "Phase 12"),
        ],
    )
    def test_selecting_a_future_provider_names_its_phase(
        self,
        monkeypatch: pytest.MonkeyPatch,
        env_var: str,
        value: str,
        builder: str,
        expected_phase: str,
    ) -> None:
        monkeypatch.setenv(env_var, value)
        # Satisfy the config validators so the failure comes from the registry.
        monkeypatch.setenv("OPENROUTER_API_KEY", "k")
        monkeypatch.setenv("OPENROUTER_MODEL", "vendor/model")
        monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "t")
        monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "1")
        monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "v")
        monkeypatch.setenv("GOOGLE_CALENDAR_ID", "cal")
        monkeypatch.setenv("GOOGLE_SECRET_MANAGER_PROJECT", "proj")
        monkeypatch.setenv("GOOGLE_CALENDAR_TOKEN_SECRET_NAME", "secret")
        monkeypatch.setenv("HUBSPOT_ACCESS_TOKEN", "t")
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj")
        monkeypatch.setenv("GOOGLE_CLOUD_TASKS_QUEUE", "queue")
        monkeypatch.setenv("WORKER_BASE_URL", "https://worker.example.com")

        builders = {
            "whatsapp": build_whatsapp_provider,
            "llm": build_llm_provider,
            "embedding": build_embedding_provider,
            "calendar": build_calendar_provider,
            "crm": build_crm_provider,
            "queue": build_queue_provider,
        }

        with pytest.raises(ProviderNotImplementedError) as exc_info:
            builders[builder](Settings())

        assert expected_phase in str(exc_info.value)

    def test_the_cloud_tasks_queue_is_now_implemented(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Phase 7 replaced the placeholder. Building it opens no connection; the
        Google client is created lazily on first use."""
        from app.integrations.queue.cloud_tasks import CloudTasksProvider

        monkeypatch.setenv("QUEUE_PROVIDER", "cloud_tasks")
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj")
        monkeypatch.setenv("GOOGLE_CLOUD_TASKS_QUEUE", "sends")
        monkeypatch.setenv("WORKER_BASE_URL", "https://worker.example.com")

        provider = build_queue_provider(Settings())
        assert isinstance(provider, CloudTasksProvider)
        assert isinstance(provider, QueueProvider)

    def test_the_meta_provider_is_now_implemented(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Phase 6 replaced the placeholder. Selecting META builds the real provider,
        which still performs no I/O until it is called."""
        from app.integrations.whatsapp.meta import MetaWhatsAppProvider

        monkeypatch.setenv("WHATSAPP_PROVIDER", "meta")
        monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "t")
        monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "1")
        monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "v")
        monkeypatch.setenv("WHATSAPP_APP_SECRET", "s")
        monkeypatch.setenv("WHATSAPP_BUSINESS_ACCOUNT_ID", "waba")

        provider = build_whatsapp_provider(Settings())
        assert isinstance(provider, MetaWhatsAppProvider)
        assert isinstance(provider, WhatsAppProvider)

    def test_meta_requires_its_webhook_credentials(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """§19: webhook signatures cannot be verified without the app secret, so a
        partial configuration must fail at startup rather than at the first webhook."""
        monkeypatch.setenv("WHATSAPP_PROVIDER", "meta")
        monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "t")
        monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "1")
        monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "v")
        with pytest.raises(ValueError, match="WHATSAPP_APP_SECRET"):
            Settings()

    def test_invalid_provider_name_is_rejected_at_config_load(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CRM_PROVIDER", "salesforce")
        with pytest.raises(ValueError, match="crm_provider"):
            Settings()
