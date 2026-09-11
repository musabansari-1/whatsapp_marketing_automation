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
from app.persistence.models import Product


def a_product(calendar_id: str = "product@calendar.example") -> Product:
    """An unsaved product carrying a calendar. The registry reads configuration only."""
    return Product(slug="registry-product", display_name="Registry", google_calendar_id=calendar_id)


class TestFakeSelection:
    def test_builds_all_six_fakes(self, settings: Settings) -> None:
        providers = build_providers(settings)
        assert isinstance(providers.whatsapp, FakeWhatsAppProvider)
        assert isinstance(providers.llm, FakeLLMProvider)
        assert isinstance(providers.embedding, FakeEmbeddingProvider)
        assert isinstance(providers.crm, FakeCRMProvider)
        assert isinstance(providers.queue, InMemoryQueueProvider)

    def test_fakes_satisfy_their_interfaces(self, settings: Settings) -> None:
        providers = build_providers(settings)
        assert isinstance(providers.whatsapp, WhatsAppProvider)
        assert isinstance(providers.llm, LLMProvider)
        assert isinstance(providers.embedding, EmbeddingProvider)
        assert isinstance(providers.crm, CRMProvider)
        assert isinstance(providers.queue, QueueProvider)

    def test_the_calendar_is_built_per_product(self, settings: Settings) -> None:
        """[D43]: there is no calendar without a product, so the product-blind bundle
        carries none and the builder requires one."""
        calendar = build_calendar_provider(settings, product=a_product("p@calendar.example"))

        assert isinstance(calendar, FakeCalendarProvider)
        assert isinstance(calendar, CalendarProvider)
        assert calendar.calendar_id == "p@calendar.example"
        assert not hasattr(build_providers(settings), "calendar")

    def test_embedding_dimension_follows_configuration(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """[D1]: the fake must match the configured pgvector width."""
        monkeypatch.setenv("EMBEDDING_DIMENSION", "512")
        assert build_embedding_provider(Settings()).dimension == 512

    def test_the_hubspot_provider_is_selectable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Phase 12 implemented it ([D11])."""
        from app.integrations.crm.hubspot import HubSpotProvider

        monkeypatch.setenv("CRM_PROVIDER", "hubspot")
        monkeypatch.setenv("HUBSPOT_ACCESS_TOKEN", "token")

        assert isinstance(build_crm_provider(Settings()), HubSpotProvider)

    def test_the_google_calendar_provider_is_selectable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Phase 11 implemented it ([D7]).

        Building must not read the secret: the registry runs in the API service too,
        which never touches the calendar, and [D19] keeps Secret Manager out of local
        development.
        """
        from app.integrations.calendar.google import GoogleCalendarProvider

        monkeypatch.setenv("CALENDAR_PROVIDER", "google")
        monkeypatch.setenv("GOOGLE_SECRET_MANAGER_PROJECT", "proj")
        monkeypatch.setenv("GOOGLE_CALENDAR_TOKEN_SECRET_NAME", "refresh-token")
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "client-id")
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "client-secret")

        provider = build_calendar_provider(Settings(), product=a_product("sales@example.com"))

        assert isinstance(provider, GoogleCalendarProvider)
        assert provider._refresh_token is None  # noqa: SLF001 - laziness is the point

    def test_the_secret_store_is_in_memory_without_the_google_calendar(self) -> None:
        """[D19]: local development and tests must not require Secret Manager."""
        from app.integrations.registry import build_secret_store
        from app.integrations.secrets import InMemorySecretStore

        assert isinstance(build_secret_store(Settings()), InMemorySecretStore)

    def test_the_openrouter_provider_is_selectable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Phase 10 implemented it ([D10])."""
        from app.integrations.llm.openrouter import OpenRouterLLMProvider

        monkeypatch.setenv("LLM_PROVIDER", "openrouter")
        monkeypatch.setenv("OPENROUTER_API_KEY", "key")
        monkeypatch.setenv("OPENROUTER_MODEL", "vendor/model")

        provider = build_llm_provider(Settings())

        assert isinstance(provider, OpenRouterLLMProvider)
        # [D17]: the model comes from configuration, never from the provider.
        assert provider.model == "vendor/model"

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


class TestEveryProviderIsImplemented:
    """Phase 12 was the last one. Every concrete provider now exists.

    This class used to assert that selecting a future provider failed with a message
    naming its phase; nothing is left to select. What remains worth testing is that
    each real provider can actually be built from configuration — a registry that
    raises `ProviderNotImplementedError` in production would take the platform down at
    startup.
    """

    def test_no_provider_raises_not_implemented(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for env_var, value in (
            ("WHATSAPP_PROVIDER", "meta"),
            ("LLM_PROVIDER", "openrouter"),
            ("EMBEDDING_PROVIDER", "local_sentence_transformer"),
            ("CALENDAR_PROVIDER", "google"),
            ("CRM_PROVIDER", "hubspot"),
            ("QUEUE_PROVIDER", "cloud_tasks"),
        ):
            monkeypatch.setenv(env_var, value)

        # Credentials the validators require. None is real.
        for env_var, value in (
            ("OPENROUTER_API_KEY", "k"),
            ("OPENROUTER_MODEL", "vendor/model"),
            ("WHATSAPP_ACCESS_TOKEN", "t"),
            ("WHATSAPP_PHONE_NUMBER_ID", "1"),
            ("WHATSAPP_VERIFY_TOKEN", "v"),
            ("WHATSAPP_BUSINESS_ACCOUNT_ID", "waba"),
            ("WHATSAPP_APP_SECRET", "s"),
            ("GOOGLE_SECRET_MANAGER_PROJECT", "proj"),
            ("GOOGLE_CALENDAR_TOKEN_SECRET_NAME", "secret"),
            ("GOOGLE_OAUTH_CLIENT_ID", "client"),
            ("GOOGLE_OAUTH_CLIENT_SECRET", "secret"),
            ("HUBSPOT_ACCESS_TOKEN", "t"),
            ("GOOGLE_CLOUD_PROJECT", "proj"),
            ("GOOGLE_CLOUD_TASKS_QUEUE", "queue"),
            ("WORKER_BASE_URL", "https://worker.example.com"),
        ):
            monkeypatch.setenv(env_var, value)

        settings = Settings()

        def build_calendar_for_a_product(settings: Settings) -> CalendarProvider:
            return build_calendar_provider(settings, product=a_product())

        builders = (
            build_whatsapp_provider,
            build_llm_provider,
            build_embedding_provider,
            build_calendar_for_a_product,
            build_crm_provider,
        )

        for builder in builders:
            try:
                builder(settings)
            except ProviderNotImplementedError as exc:  # pragma: no cover - the assertion
                pytest.fail(f"{builder.__name__} is still unimplemented: {exc}")

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
