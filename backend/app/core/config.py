"""Application configuration.

Every externally variable value in the system lands here. Per CLAUDE.md, the LLM
model, embedding model and vector dimension are never hard-coded, and neither are
slot policy values or customer-facing copy.

Provider selection is configuration too, so no layer above `app.integrations`
ever names a concrete provider (CLAUDE.md).
"""

from __future__ import annotations

import functools
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class AppEnv(StrEnum):
    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class WhatsAppProviderName(StrEnum):
    FAKE = "fake"
    META = "meta"


class LLMProviderName(StrEnum):
    FAKE = "fake"
    OPENROUTER = "openrouter"


class EmbeddingProviderName(StrEnum):
    FAKE = "fake"
    LOCAL_SENTENCE_TRANSFORMER = "local_sentence_transformer"


class CalendarProviderName(StrEnum):
    FAKE = "fake"
    GOOGLE = "google"


class CRMProviderName(StrEnum):
    FAKE = "fake"
    HUBSPOT = "hubspot"


class QueueProviderName(StrEnum):
    INLINE = "inline"
    CLOUD_TASKS = "cloud_tasks"


# NoDecode stops pydantic-settings from JSON-decoding the raw env value, so plain
# comma-separated lists work (`OPT_OUT_KEYWORDS=STOP,UNSUBSCRIBE`) rather than
# requiring JSON. The `_parse_csv` validator below does the splitting.
CommaSeparated = Annotated[list[str], NoDecode]


def _split_csv(value: object) -> object:
    """Accept both a comma-separated string and a real list for list-valued settings."""
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------------------------------------------------------- core
    app_env: AppEnv = AppEnv.LOCAL
    app_secret: SecretStr = SecretStr("dev-insecure-change-me")
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "console"

    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/whatsapp_marketing"
    database_echo: bool = False
    database_pool_size: int = 5
    database_max_overflow: int = 5

    frontend_url: str = "http://localhost:3000"
    api_url: str = "http://localhost:8000"

    # ---------------------------------------------------------------- session [D2]
    session_cookie_name: str = "wam_session"
    session_cookie_secure: bool = True
    # Lax, not None. [D13] routes the browser through the same-origin BFF, so the
    # cookie is same-site and never needs the SameSite=None that [D2] rejects.
    session_cookie_samesite: Literal["lax", "strict"] = "lax"
    session_ttl_seconds: int = 60 * 60 * 8

    # ---------------------------------------------------------------- bootstrap [D15]
    # Credentials for the one-time first-ADMIN command. Absent in normal operation;
    # supplied from secret storage only while bootstrapping.
    bootstrap_admin_email: str | None = None
    bootstrap_admin_password: SecretStr | None = None

    # ---------------------------------------------------------------- provider selection
    whatsapp_provider: WhatsAppProviderName = WhatsAppProviderName.FAKE
    llm_provider: LLMProviderName = LLMProviderName.FAKE
    embedding_provider: EmbeddingProviderName = EmbeddingProviderName.FAKE
    calendar_provider: CalendarProviderName = CalendarProviderName.FAKE
    crm_provider: CRMProviderName = CRMProviderName.FAKE
    queue_provider: QueueProviderName = QueueProviderName.INLINE

    # ---------------------------------------------------------------- WhatsApp
    whatsapp_access_token: SecretStr | None = None
    whatsapp_phone_number_id: str | None = None
    whatsapp_verify_token: SecretStr | None = None

    # ---------------------------------------------------------------- LLM [D10][D17]
    openrouter_api_key: SecretStr | None = None
    # No default. [D17]: the model is chosen after Phase 10 benchmarking.
    openrouter_model: str | None = None

    # ---------------------------------------------------------------- embeddings [D1]
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    embedding_dimension: int = 384

    # ---------------------------------------------------------------- Google Cloud
    google_cloud_project: str | None = None
    google_cloud_location: str = "us-central1"
    google_cloud_tasks_queue: str | None = None
    worker_base_url: str | None = None
    worker_service_account_email: str | None = None

    # ---------------------------------------------------------------- Calendar [D7][D19]
    google_calendar_id: str | None = None
    google_secret_manager_project: str | None = None
    google_calendar_token_secret_name: str | None = None

    # ---------------------------------------------------------------- CRM
    hubspot_access_token: SecretStr | None = None

    # ---------------------------------------------------------------- audience [D4][D20]
    # The production Excel schema is deliberately absent; the client supplies it
    # before Phase 4 and it must not be invented.
    audience_phone_default_region: str = "IN"

    # ---------------------------------------------------------------- opt-out [D5]
    opt_out_keywords: CommaSeparated = Field(
        default_factory=lambda: ["STOP", "UNSUBSCRIBE", "OPT OUT", "OPTOUT"]
    )

    # ---------------------------------------------------------------- slot policy [D8]
    demo_duration_minutes: int = 30
    demo_buffer_minutes: int = 15
    demo_working_days: CommaSeparated = Field(
        default_factory=lambda: ["MON", "TUE", "WED", "THU", "FRI"]
    )
    demo_working_hours_start: str = "10:00"
    demo_working_hours_end: str = "18:00"
    demo_booking_horizon_days: int = 14
    demo_timezone: str = "Asia/Kolkata"

    # ---------------------------------------------------------------- qualification [D9][D18]
    qualification_required_fields: CommaSeparated = Field(default_factory=list)

    # ---------------------------------------------------------------- customer copy [D21]
    # Never hard-coded in application logic.
    msg_rag_fallback: str = (
        "I don't have verified information on that yet. "
        "Let me have someone from our team follow up with you."
    )
    msg_booking_confirmation: str = "Your demo is confirmed for {start_time} ({timezone})."
    msg_opt_out_response: str = "You have been unsubscribed and will not receive further messages."

    # ---------------------------------------------------------------- validators

    @field_validator(
        "opt_out_keywords",
        "demo_working_days",
        "qualification_required_fields",
        mode="before",
    )
    @classmethod
    def _parse_csv(cls, value: object) -> object:
        return _split_csv(value)

    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, value: str) -> str:
        normalized = value.upper()
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}
        if normalized not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}")
        return normalized

    @field_validator("embedding_dimension")
    @classmethod
    def _positive_dimension(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("embedding_dimension must be positive")
        return value

    @model_validator(mode="after")
    def _validate_selected_providers(self) -> Settings:
        """Fail fast when a selected provider is missing its required configuration.

        This is what makes [D17] real: nothing defaults the model, and choosing the
        OpenRouter provider without one is a startup error rather than a runtime
        surprise. Fake providers need no credentials, which is why local development
        and tests start with no secrets present.
        """
        missing: list[str] = []

        if self.llm_provider is LLMProviderName.OPENROUTER:
            if not self.openrouter_api_key:
                missing.append("OPENROUTER_API_KEY")
            if not self.openrouter_model:
                missing.append("OPENROUTER_MODEL")

        if self.whatsapp_provider is WhatsAppProviderName.META:
            if not self.whatsapp_access_token:
                missing.append("WHATSAPP_ACCESS_TOKEN")
            if not self.whatsapp_phone_number_id:
                missing.append("WHATSAPP_PHONE_NUMBER_ID")
            if not self.whatsapp_verify_token:
                missing.append("WHATSAPP_VERIFY_TOKEN")

        if self.calendar_provider is CalendarProviderName.GOOGLE:
            if not self.google_calendar_id:
                missing.append("GOOGLE_CALENDAR_ID")
            if not self.google_secret_manager_project:
                missing.append("GOOGLE_SECRET_MANAGER_PROJECT")
            if not self.google_calendar_token_secret_name:
                missing.append("GOOGLE_CALENDAR_TOKEN_SECRET_NAME")

        if self.crm_provider is CRMProviderName.HUBSPOT and not self.hubspot_access_token:
            missing.append("HUBSPOT_ACCESS_TOKEN")

        if self.queue_provider is QueueProviderName.CLOUD_TASKS:
            if not self.google_cloud_project:
                missing.append("GOOGLE_CLOUD_PROJECT")
            if not self.google_cloud_tasks_queue:
                missing.append("GOOGLE_CLOUD_TASKS_QUEUE")
            if not self.worker_base_url:
                missing.append("WORKER_BASE_URL")

        if missing:
            raise ValueError(
                "Missing required configuration for the selected providers: "
                + ", ".join(sorted(set(missing)))
            )
        return self

    @model_validator(mode="after")
    def _validate_production_hardening(self) -> Settings:
        if self.app_env is not AppEnv.PRODUCTION:
            return self
        if self.app_secret.get_secret_value() == "dev-insecure-change-me":
            raise ValueError("APP_SECRET must be set to a real secret in production")
        if not self.session_cookie_secure:
            raise ValueError("SESSION_COOKIE_SECURE cannot be disabled in production")
        return self

    # ---------------------------------------------------------------- helpers

    @property
    def is_production(self) -> bool:
        return self.app_env is AppEnv.PRODUCTION

    @property
    def is_test(self) -> bool:
        return self.app_env is AppEnv.TEST


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton.

    Cached so configuration is read and validated once. Tests clear the cache via
    the `settings` fixture rather than mutating the instance.
    """
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
