"""Configuration tests.

The load-bearing case is fail-fast: [D17] requires that selecting OpenRouter without
a model is a startup error, and CLAUDE.md forbids a baked-in default.
"""

from __future__ import annotations

import pytest

from app.core.config import (
    AppEnv,
    CalendarProviderName,
    CRMProviderName,
    LLMProviderName,
    QueueProviderName,
    Settings,
    WhatsAppProviderName,
    get_settings,
    reset_settings_cache,
)


class TestProviderDefaults:
    def test_defaults_to_fake_providers(self, settings: Settings) -> None:
        assert settings.whatsapp_provider is WhatsAppProviderName.FAKE
        assert settings.llm_provider is LLMProviderName.FAKE
        assert settings.calendar_provider is CalendarProviderName.FAKE
        assert settings.crm_provider is CRMProviderName.FAKE
        assert settings.queue_provider is QueueProviderName.INLINE

    def test_fake_providers_need_no_credentials(self, settings: Settings) -> None:
        """The suite and local development must start with no secrets present."""
        assert settings.openrouter_api_key is None
        assert settings.whatsapp_access_token is None
        assert settings.hubspot_access_token is None


class TestModelIsNotHardCoded:
    def test_no_default_llm_model(self, settings: Settings) -> None:
        """[D17]: nothing may pre-select a model."""
        assert settings.openrouter_model is None

    def test_openrouter_without_model_fails_fast(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_PROVIDER", "openrouter")
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
        with pytest.raises(ValueError, match="OPENROUTER_MODEL"):
            Settings()

    def test_openrouter_without_api_key_fails_fast(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_PROVIDER", "openrouter")
        monkeypatch.setenv("OPENROUTER_MODEL", "some/model")
        with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
            Settings()

    def test_openrouter_with_both_is_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_PROVIDER", "openrouter")
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
        monkeypatch.setenv("OPENROUTER_MODEL", "vendor/cheap-model")
        settings = Settings()
        assert settings.openrouter_model == "vendor/cheap-model"


class TestProviderConfigValidation:
    def test_meta_whatsapp_requires_credentials(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WHATSAPP_PROVIDER", "meta")
        with pytest.raises(ValueError, match="WHATSAPP_ACCESS_TOKEN"):
            Settings()

    def test_google_calendar_requires_secret_manager_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """[D19]: the refresh token lives in Secret Manager, so its location is required."""
        monkeypatch.setenv("CALENDAR_PROVIDER", "google")
        monkeypatch.setenv("GOOGLE_CALENDAR_ID", "sales@example.com")
        with pytest.raises(ValueError, match="GOOGLE_SECRET_MANAGER_PROJECT"):
            Settings()

    def test_hubspot_requires_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CRM_PROVIDER", "hubspot")
        with pytest.raises(ValueError, match="HUBSPOT_ACCESS_TOKEN"):
            Settings()

    def test_cloud_tasks_requires_project_and_queue(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("QUEUE_PROVIDER", "cloud_tasks")
        with pytest.raises(ValueError, match="GOOGLE_CLOUD_TASKS_QUEUE"):
            Settings()

    def test_error_lists_every_missing_variable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WHATSAPP_PROVIDER", "meta")
        with pytest.raises(ValueError) as exc_info:
            Settings()
        message = str(exc_info.value)
        assert "WHATSAPP_ACCESS_TOKEN" in message
        assert "WHATSAPP_PHONE_NUMBER_ID" in message
        assert "WHATSAPP_VERIFY_TOKEN" in message


class TestEmbeddingConfig:
    def test_dimension_matches_initial_model(self, settings: Settings) -> None:
        """[D1]: all-MiniLM-L6-v2 is 384-dimensional."""
        assert settings.embedding_dimension == 384
        assert settings.embedding_model == "sentence-transformers/all-MiniLM-L6-v2"

    def test_dimension_is_configurable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EMBEDDING_DIMENSION", "768")
        monkeypatch.setenv("EMBEDDING_MODEL", "some/other-model")
        settings = Settings()
        assert settings.embedding_dimension == 768
        assert settings.embedding_model == "some/other-model"

    def test_dimension_must_be_positive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EMBEDDING_DIMENSION", "0")
        with pytest.raises(ValueError, match="positive"):
            Settings()


class TestAudienceSchema:
    def test_placeholder_is_flagged(self, settings: Settings) -> None:
        """[D20]: a development default must never look authoritative."""
        assert settings.audience_schema_is_placeholder is True

    def test_schema_parses_into_column_mappings(self, settings: Settings) -> None:
        schema = settings.audience_schema()
        assert any(column.field.value == "phone" and column.required for column in schema.columns)

    def test_client_schema_replaces_it_without_code_changes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "AUDIENCE_COLUMN_SCHEMA",
            '[{"source": "Full Name", "field": "name", "required": true},'
            ' {"source": "Mobile", "field": "phone", "required": true}]',
        )
        settings = Settings()
        assert [c.source for c in settings.audience_schema().columns] == ["Full Name", "Mobile"]
        assert settings.audience_schema_is_placeholder is False

    def test_a_schema_without_a_phone_column_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "AUDIENCE_COLUMN_SCHEMA",
            '[{"source": "Name", "field": "name", "required": true}]',
        )
        with pytest.raises(ValueError, match="phone column"):
            Settings().audience_schema()

    def test_the_placeholder_is_the_canonical_field_set(self, settings) -> None:  # type: ignore[no-untyped-def]
        """[D25]/[D32]: the placeholder differs from production only in its header
        names, never in which canonical fields exist."""
        fields = {column.field.value for column in settings.audience_schema().columns}
        assert fields == {"name", "phone", "email", "company", "designation", "city"}


class TestSlotPolicy:
    def test_defaults_match_d8(self, settings: Settings) -> None:
        assert settings.demo_duration_minutes == 30
        assert settings.demo_buffer_minutes == 15
        assert settings.demo_working_days == ["MON", "TUE", "WED", "THU", "FRI"]
        assert settings.demo_working_hours_start == "10:00"
        assert settings.demo_working_hours_end == "18:00"
        assert settings.demo_booking_horizon_days == 14

    def test_working_days_parse_from_csv(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DEMO_WORKING_DAYS", "MON, WED , FRI")
        assert Settings().demo_working_days == ["MON", "WED", "FRI"]

    def test_slot_policy_is_configurable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DEMO_DURATION_MINUTES", "45")
        monkeypatch.setenv("DEMO_TIMEZONE", "Europe/London")
        settings = Settings()
        assert settings.demo_duration_minutes == 45
        assert settings.demo_timezone == "Europe/London"


class TestOptOutAndAudience:
    def test_opt_out_keywords_have_defaults(self, settings: Settings) -> None:
        assert "STOP" in settings.opt_out_keywords

    def test_opt_out_keywords_are_configurable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPT_OUT_KEYWORDS", "STOP,BAND KARO")
        assert Settings().opt_out_keywords == ["STOP", "BAND KARO"]

    def test_phone_region_defaults_to_india(self, settings: Settings) -> None:
        """[D4]: Indian numbers by default."""
        assert settings.audience_phone_default_region == "IN"


class TestCustomerFacingCopy:
    def test_copy_is_configurable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """[D21]: never hard-coded in application logic."""
        monkeypatch.setenv("MSG_RAG_FALLBACK", "Custom fallback.")
        assert Settings().msg_rag_fallback == "Custom fallback."


class TestSuiteIsolation:
    def test_the_suite_does_not_read_a_developers_env_file(self) -> None:
        """`Settings` names `.env`; the suite must not.

        Without this, every assertion about a *default* silently becomes an assertion
        about whatever `backend/.env` happens to contain — green in CI, which has no
        such file, and red on a developer's machine, which does.
        """
        assert Settings.model_config["env_file"] is None


class TestProductionHardening:
    def test_production_rejects_default_secret(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APP_ENV", "production")
        with pytest.raises(ValueError, match="APP_SECRET"):
            Settings()

    def test_production_rejects_insecure_cookie(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("APP_SECRET", "a-real-secret")
        monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
        with pytest.raises(ValueError, match="SESSION_COOKIE_SECURE"):
            Settings()

    def test_production_requires_a_real_audience_schema(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """[D4]/[D20]: the development placeholder must never reach production.

        This is what makes it safe to build the audience pipeline before the client
        confirms their column names.
        """
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("APP_SECRET", "a-real-secret")
        with pytest.raises(ValueError, match="AUDIENCE_COLUMN_SCHEMA"):
            Settings()

    def test_production_refuses_a_disabled_rate_limiter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """§19 requires rate limits, and `RATE_LIMIT_ENABLED` exists only for tests.

        The guard shipped with Phase 14a untested, which is the state in which a
        production gate quietly disappears in a refactor.
        """
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("APP_SECRET", "a-real-secret")
        monkeypatch.setenv(
            "AUDIENCE_COLUMN_SCHEMA", '[{"source": "Mobile", "field": "phone", "required": true}]'
        )
        monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
        with pytest.raises(ValueError, match="RATE_LIMIT_ENABLED"):
            Settings()

    def test_production_accepts_hardened_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("APP_SECRET", "a-real-secret")
        monkeypatch.setenv(
            "AUDIENCE_COLUMN_SCHEMA", '[{"source": "Mobile", "field": "phone", "required": true}]'
        )
        settings = Settings()
        assert settings.is_production
        assert settings.app_env is AppEnv.PRODUCTION
        assert settings.audience_schema_is_placeholder is False


class TestSettingsCache:
    def test_get_settings_is_cached(self) -> None:
        assert get_settings() is get_settings()

    def test_cache_can_be_reset(self) -> None:
        first = get_settings()
        reset_settings_cache()
        assert get_settings() is not first


class TestSecretsAreNotLeaked:
    def test_repr_masks_secrets(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """§19: never log provider access tokens."""
        monkeypatch.setenv("HUBSPOT_ACCESS_TOKEN", "super-secret-token")
        monkeypatch.setenv("CRM_PROVIDER", "hubspot")
        settings = Settings()
        assert "super-secret-token" not in repr(settings)
        assert settings.hubspot_access_token is not None
        assert settings.hubspot_access_token.get_secret_value() == "super-secret-token"


class TestUploadLimitFitsTheQueue:
    """V1 sends document bytes inside the task payload, and Cloud Tasks caps a task at
    1 MB. A limit the queue cannot carry would fail at enqueue time — after the user
    has already been told the upload succeeded."""

    def _cloud_tasks_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("QUEUE_PROVIDER", "cloud_tasks")
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj")
        monkeypatch.setenv("GOOGLE_CLOUD_TASKS_QUEUE", "queue")
        monkeypatch.setenv("WORKER_BASE_URL", "https://worker.example.com")

    def test_the_default_limit_fits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._cloud_tasks_env(monkeypatch)
        assert Settings().knowledge_max_file_bytes <= Settings.CLOUD_TASKS_SAFE_UPLOAD_BYTES

    def test_an_oversized_limit_is_refused_at_startup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._cloud_tasks_env(monkeypatch)
        monkeypatch.setenv("KNOWLEDGE_MAX_FILE_BYTES", str(10 * 1024 * 1024))

        with pytest.raises(ValueError, match="object storage"):
            Settings()

    def test_the_in_memory_queue_is_unconstrained(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Development and tests have no such limit; capping there would make local
        work harder for no reason."""
        monkeypatch.setenv("QUEUE_PROVIDER", "inline")
        monkeypatch.setenv("KNOWLEDGE_MAX_FILE_BYTES", str(50 * 1024 * 1024))

        assert Settings().knowledge_max_file_bytes == 50 * 1024 * 1024
