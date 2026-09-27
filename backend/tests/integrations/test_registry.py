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
    WhatsAppNotConfiguredError,
    build_calendar_auth,
    build_calendar_provider,
    build_crm_provider,
    build_embedding_provider,
    build_llm_provider,
    build_providers,
    build_queue_provider,
    build_whatsapp_provider,
    build_whatsapp_template_source,
)
from app.integrations.whatsapp import FakeWhatsAppProvider, WhatsAppProvider
from app.persistence.models import Product


def a_product(calendar_id: str = "product@calendar.example") -> Product:
    """An unsaved product carrying a calendar and a WhatsApp number. The registry reads
    configuration only."""
    return Product(
        slug="registry-product",
        display_name="Registry",
        google_calendar_id=calendar_id,
        whatsapp_phone_number_id="registry-number",
        whatsapp_business_account_id="registry-account",
    )


class TestFakeSelection:
    def test_builds_the_product_blind_fakes(self, settings: Settings) -> None:
        providers = build_providers(settings)
        assert isinstance(providers.llm, FakeLLMProvider)
        assert isinstance(providers.embedding, FakeEmbeddingProvider)
        assert isinstance(providers.crm, FakeCRMProvider)
        assert isinstance(providers.queue, InMemoryQueueProvider)

    def test_fakes_satisfy_their_interfaces(self, settings: Settings) -> None:
        providers = build_providers(settings)
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

    def test_the_calendar_carries_the_product_meeting_link(self, settings: Settings) -> None:
        """Option 2 ([D65]): the product's permanent link reaches its calendar."""
        product = a_product("p@calendar.example")
        product.meeting_url = "https://meet.google.com/static-room-abc"

        calendar = build_calendar_provider(settings, product=product)

        assert isinstance(calendar, FakeCalendarProvider)
        assert calendar.static_meeting_link == "https://meet.google.com/static-room-abc"

    def test_whatsapp_is_built_per_product(self, settings: Settings) -> None:
        """[D40]: a number belongs to a product, so the product-blind bundle carries no
        WhatsApp provider and the builder requires the product."""
        provider = build_whatsapp_provider(settings, product=a_product())

        assert isinstance(provider, FakeWhatsAppProvider)
        assert isinstance(provider, WhatsAppProvider)
        assert not hasattr(build_providers(settings), "whatsapp")

    def test_a_product_without_a_number_is_refused_even_by_the_fake(
        self, settings: Settings
    ) -> None:
        """Checked before a provider is chosen, so the suite exercises the refusal that
        production relies on. There is no platform number to fall back to (Phase 27)."""
        unconfigured = Product(slug="no-number", display_name="No number")

        with pytest.raises(WhatsAppNotConfiguredError, match="no-number"):
            build_whatsapp_provider(settings, product=unconfigured)

    def test_a_number_without_its_business_account_is_refused(self, settings: Settings) -> None:
        half = Product(slug="half", display_name="Half", whatsapp_phone_number_id="1")

        with pytest.raises(WhatsAppNotConfiguredError):
            build_whatsapp_provider(settings, product=half)

    def test_template_sync_reads_an_account_and_takes_no_product(self, settings: Settings) -> None:
        """[D57]: templates belong to a business account; ownership is decided later."""
        source = build_whatsapp_template_source(settings, business_account_id="shared")

        assert isinstance(source, FakeWhatsAppProvider)

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
        # Laziness is the point: nothing has read a secret yet.
        assert provider._auth._refresh_token is None  # noqa: SLF001

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


class TestCalendarAuthMode:
    """[D62]: which way the platform proves its identity to Google.

    The two modes differ operationally, not functionally — the same calendar, the same
    calls — so what matters here is that selecting one builds the right authenticator and
    demands only the configuration that mode actually reads.
    """

    def test_the_default_is_the_refresh_token(self, settings: Settings) -> None:
        """Unchanged behaviour: an environment that sets nothing new keeps working."""
        from app.integrations.calendar.google_auth import OAuthRefreshTokenAuthenticator

        assert settings.google_calendar_auth_mode.value == "oauth_refresh_token"

        auth = build_calendar_auth(settings, product=a_product())

        assert isinstance(auth, OAuthRefreshTokenAuthenticator)

    def test_service_account_mode_builds_the_service_account_authenticator(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.integrations.calendar.google_auth import (
            CALENDAR_SCOPE,
            ServiceAccountAuthenticator,
        )

        monkeypatch.setenv("CALENDAR_PROVIDER", "google")
        monkeypatch.setenv("GOOGLE_CALENDAR_AUTH_MODE", "service_account")

        # No OAuth client, no Secret Manager project, no secret name — and it still
        # builds, because none of them is read in this mode.
        settings = Settings()
        auth = build_calendar_auth(settings, product=a_product())

        assert isinstance(auth, ServiceAccountAuthenticator)
        assert auth._scopes == (CALENDAR_SCOPE,)  # noqa: SLF001
        # Nothing resolved Application Default Credentials to do it.
        assert auth._credentials is None  # noqa: SLF001

    def test_service_account_mode_selects_the_real_provider_without_a_secret(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.integrations.calendar.google import GoogleCalendarProvider
        from app.integrations.calendar.google_auth import ServiceAccountAuthenticator

        monkeypatch.setenv("CALENDAR_PROVIDER", "google")
        monkeypatch.setenv("GOOGLE_CALENDAR_AUTH_MODE", "service_account")

        provider = build_calendar_provider(Settings(), product=a_product("sales@example.com"))

        assert isinstance(provider, GoogleCalendarProvider)
        assert isinstance(provider._auth, ServiceAccountAuthenticator)  # noqa: SLF001

    def test_the_refresh_token_mode_still_requires_its_credentials(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The check that has always existed, scoped to the mode that needs it."""
        monkeypatch.setenv("CALENDAR_PROVIDER", "google")
        monkeypatch.setenv("GOOGLE_CALENDAR_AUTH_MODE", "oauth_refresh_token")

        with pytest.raises(ValueError, match="GOOGLE_OAUTH_CLIENT_ID"):
            Settings()

    def test_an_unknown_auth_mode_is_rejected_at_config_load(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CALENDAR_PROVIDER", "google")
        monkeypatch.setenv("GOOGLE_CALENDAR_AUTH_MODE", "workload_identity")

        with pytest.raises(ValueError, match="google_calendar_auth_mode"):
            Settings()

    def test_a_product_may_still_name_its_own_token_secret(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """[D43] is unchanged: one shared OAuth client, one token per Google account."""
        monkeypatch.setenv("CALENDAR_PROVIDER", "google")
        monkeypatch.setenv("GOOGLE_SECRET_MANAGER_PROJECT", "proj")
        monkeypatch.setenv("GOOGLE_CALENDAR_TOKEN_SECRET_NAME", "shared-refresh-token")
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "client-id")
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "client-secret")

        own = a_product()
        own.google_calendar_token_secret_name = "herba-refresh-token"
        shared = build_calendar_auth(Settings(), product=a_product())
        separate = build_calendar_auth(Settings(), product=own)

        assert shared._client_id == separate._client_id == "client-id"  # noqa: SLF001
        assert shared._refresh_token_secret_name == "shared-refresh-token"  # noqa: SLF001
        assert separate._refresh_token_secret_name == "herba-refresh-token"  # noqa: SLF001


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
            ("WHATSAPP_VERIFY_TOKEN", "v"),
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

        def build_whatsapp_for_a_product(settings: Settings) -> WhatsAppProvider:
            return build_whatsapp_provider(settings, product=a_product())

        builders = (
            build_whatsapp_for_a_product,
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
        monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "v")
        monkeypatch.setenv("WHATSAPP_APP_SECRET", "s")

        provider = build_whatsapp_provider(Settings(), product=a_product())
        assert isinstance(provider, MetaWhatsAppProvider)
        assert isinstance(provider, WhatsAppProvider)
        # The product's number and account, never a platform-wide one.
        assert provider._phone_number_id == "registry-number"  # noqa: SLF001
        assert provider._business_account_id == "registry-account"  # noqa: SLF001

        # A template source reads an account and has no number to send from.
        source = build_whatsapp_template_source(Settings(), business_account_id="shared")
        assert isinstance(source, MetaWhatsAppProvider)
        assert source._phone_number_id is None  # noqa: SLF001

    def test_meta_requires_its_webhook_credentials(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """§19: webhook signatures cannot be verified without the app secret, so a
        partial configuration must fail at startup rather than at the first webhook."""
        monkeypatch.setenv("WHATSAPP_PROVIDER", "meta")
        monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "t")
        monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "v")
        with pytest.raises(ValueError, match="WHATSAPP_APP_SECRET"):
            Settings()

    def test_invalid_provider_name_is_rejected_at_config_load(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CRM_PROVIDER", "salesforce")
        with pytest.raises(ValueError, match="crm_provider"):
            Settings()
