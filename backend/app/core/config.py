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
from typing import TYPE_CHECKING, Annotated, ClassVar, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

if TYPE_CHECKING:
    from app.domain.audience import AudienceColumnSchema


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

# The development-only audience schema. Production must replace it; the value is named
# here so the production guard can detect that it has not been.
PLACEHOLDER_AUDIENCE_COLUMN_SCHEMA = (
    '[{"source": "Name", "field": "name", "required": true},'
    ' {"source": "Phone", "field": "phone", "required": true},'
    ' {"source": "Email", "field": "email", "required": false},'
    ' {"source": "Company", "field": "company", "required": false},'
    ' {"source": "Designation", "field": "designation", "required": false},'
    ' {"source": "City", "field": "city", "required": false}]'
)


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
    # Meta signs webhook payloads with the App Secret (HMAC-SHA256). §19 requires
    # webhooks to be authenticated, and §24 does not list this variable, so it is a
    # documented addition rather than an invented requirement.
    whatsapp_app_secret: SecretStr | None = None
    # Needed to list message templates, which hang off the WABA rather than the
    # phone number.
    whatsapp_business_account_id: str | None = None
    whatsapp_api_version: str = "v21.0"
    whatsapp_api_base_url: str = "https://graph.facebook.com"
    whatsapp_timeout_seconds: float = 15.0

    # ---------------------------------------------------------------- LLM [D10][D17]
    openrouter_api_key: SecretStr | None = None
    # No default. [D17]: the model is chosen after Phase 10 benchmarking.
    openrouter_model: str | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    # Generous next to the WhatsApp timeout: a model generating a long reply
    # legitimately takes tens of seconds, and cutting it off wastes what was spent.
    openrouter_timeout_seconds: float = 60.0

    # ---------------------------------------------------------------- AI agent §9 §11
    # How many rounds of tool calls one turn may take. Enough for look up the lead →
    # record facts → fetch slots → book, and short enough that a confused model cannot
    # loop at the customer's expense.
    ai_max_tool_rounds: int = 4
    # Whether the agent retrieves knowledge before replying. Off leaves it answering
    # from the conversation alone, which is the safe behaviour before the client's
    # documents are loaded (§11).
    ai_retrieval_enabled: bool = True
    # **No longer read at runtime.** Phase 21 ([D56]) made knowledge ownership come from
    # the product relationship, so selecting one platform-wide base by name is
    # meaningless — and its "use the only base" fallback was the multi-product leak.
    #
    # Kept as a field only so an existing `.env` still parses. Retiring it from
    # `.env.example` and the deployment scripts is Phase 27's configuration cleanup;
    # nothing in `app/` reads it, and nothing may start.
    ai_knowledge_base_name: str | None = None

    # ---------------------------------------------------------------- embeddings [D1]
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    embedding_dimension: int = 384
    embedding_batch_size: int = 32

    # ---------------------------------------------------------------- RAG [§11]
    # Chunk size is bounded by the embedding model's input window, not by taste:
    # all-MiniLM-L6-v2 silently truncates past 256 word-pieces, so an over-long chunk
    # is embedded from its opening words alone.
    rag_chunk_chars: int = 800
    rag_chunk_overlap_chars: int = 120
    rag_top_k: int = 5
    # Below this cosine similarity the platform reports insufficient evidence and the
    # caller uses the approved fallback rather than answering (§11).
    rag_min_similarity: float = 0.25
    rag_max_context_chars: int = 4000
    # V1 has no blob store, so an uploaded document travels base64-encoded inside the
    # Cloud Tasks payload. Cloud Tasks caps a task at 1 MB and base64 inflates by a
    # third, so the default is what actually fits. Raising it requires object storage;
    # a validator below refuses the broken combination rather than letting a large
    # upload fail at enqueue time, after the user has already been told it worked.
    knowledge_max_file_bytes: int = 700_000

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
    # The OAuth client the refresh token belongs to. The *token* lives in Secret
    # Manager ([D19]); these identify the application it was granted to, and the
    # secret is still a secret — hence SecretStr.
    google_oauth_client_id: str | None = None
    google_oauth_client_secret: SecretStr | None = None
    google_calendar_timeout_seconds: float = 20.0

    # ---------------------------------------------------------------- CRM
    hubspot_access_token: SecretStr | None = None
    hubspot_base_url: str = "https://api.hubapi.com"
    hubspot_timeout_seconds: float = 20.0

    # ---------------------------------------------------------------- audience [D4][D20]
    audience_phone_default_region: str = "IN"

    # Maps the client's spreadsheet headers onto the canonical fields ([D25]). The
    # canonical set is fixed; only this mapping varies per client. The value below uses
    # the canonical names as headers, which suits development and tests; production
    # refuses to start with it (see _validate_production_hardening), so a real mapping
    # must always be supplied ([D32]).
    audience_column_schema: str = PLACEHOLDER_AUDIENCE_COLUMN_SCHEMA
    audience_max_file_bytes: int = 5 * 1024 * 1024
    audience_max_rows: int = 50_000

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

    # ---------------------------------------------------------------- conversations §9 §10
    # How many prior turns are sent to the model. A context window is finite and the
    # recent turns are what the next reply depends on; tune against real conversations
    # in Phase 10 alongside the model choice ([D17]).
    conversation_history_turns: int = 20

    # ---------------------------------------------------------------- rate limits §19
    # §19 asks for "appropriate rate limits" without saying what they are, so every
    # threshold is configuration. Off only for a test that needs to make many attempts;
    # production hardening refuses a disabled limiter.
    rate_limit_enabled: bool = True
    # Login: two scopes covering each other's gap. Per-IP is defeated by a distributed
    # attempt and can lock out an office behind one address; per-account protects a
    # known user but lets an attacker lock them out.
    rate_limit_login_ip_attempts: int = 20
    rate_limit_login_account_attempts: int = 8
    rate_limit_login_window_seconds: int = 900
    # Generous, because Meta legitimately bursts webhook deliveries and refusing a real
    # one costs a customer message.
    rate_limit_webhook_requests: int = 600
    rate_limit_webhook_window_seconds: int = 60
    # Tighter: uploads are authenticated and each one costs embedding work.
    rate_limit_upload_requests: int = 30
    rate_limit_upload_window_seconds: int = 3600

    # ---------------------------------------------------------------- sweeps §20
    # How old an inbound message must be before the sweep treats it as stranded rather
    # than in flight. Too short and the sweep races the task already on its way; too
    # long and a customer waits that much longer for a reply that was lost.
    sweep_unanswered_grace_seconds: int = 600
    # Cap per sweep run, so a backlog is worked through in batches rather than
    # enqueueing thousands of tasks at once.
    sweep_batch_size: int = 200

    # ---------------------------------------------------------------- customer copy [D21]
    # Never hard-coded in application logic.
    msg_rag_fallback: str = (
        "I don't have verified information on that yet. "
        "Let me have someone from our team follow up with you."
    )
    msg_booking_confirmation: str = "Your demo is confirmed for {start_time} ({timezone})."
    msg_opt_out_response: str = "You have been unsubscribed and will not receive further messages."
    # The AI's operating instructions. Configuration ([D21]) because it is
    # customer-facing behaviour, and because Phase 10 will iterate on it against real
    # conversations. Deliberately conservative: it tells the model what it may not do,
    # since the tools and retrieval that let it do more arrive in Phase 10.
    ai_system_prompt: str = (
        "You are a helpful assistant replying to a customer on WhatsApp on behalf of "
        "the business. Be brief and professional. Only state facts that appear in the "
        "conversation or in verified information provided to you. If you do not know "
        "something, say you will have a colleague follow up. Never invent prices, "
        "availability, or commitments."
    )

    # ---------------------------------------------------------------- retries [D27]
    # Maximum attempts per recipient, counting the first. Configuration, never a
    # literal, so operations can tune it without a deploy.
    send_max_attempts: int = 5
    send_backoff_base_seconds: float = 2.0
    send_backoff_max_seconds: float = 300.0
    # Fraction of the delay applied as random jitter. Without it, a batch that fails
    # together retries together, reproducing the same spike that caused the failure.
    send_backoff_jitter: float = 0.25

    # ---------------------------------------------------------------- dispatch [D28]
    # No default: [D28] forbids assuming a messaging tier. Unset means no client-side
    # pacing, which is correct for development and for a queue configured to pace
    # itself. Production sets it from the WhatsApp account's actual limits.
    dispatch_rate_per_second: float | None = None
    dispatch_max_concurrent: int = 10

    # ---------------------------------------------------------------- templates [D24]
    # Purpose -> approved template name. Names only: the content is written and
    # approved by the client through Meta and synchronized in ([D6]).
    template_booking_confirmation: str | None = None
    template_window_fallback: str | None = None

    # ---------------------------------------------------------------- messaging window
    # Meta only permits free-form (non-template) replies within 24 hours of the
    # customer's last inbound message. Configurable because Meta has changed it before.
    customer_service_window_hours: int = 24

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

    @field_validator("send_max_attempts")
    @classmethod
    def _at_least_one_attempt(cls, value: int) -> int:
        if value < 1:
            raise ValueError("send_max_attempts must be at least 1")
        return value

    @field_validator("send_backoff_jitter")
    @classmethod
    def _jitter_is_a_fraction(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError("send_backoff_jitter must be between 0 and 1")
        return value

    @field_validator("dispatch_rate_per_second")
    @classmethod
    def _positive_rate(cls, value: float | None) -> float | None:
        if value is not None and value <= 0:
            raise ValueError("dispatch_rate_per_second must be positive when set")
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
            # Without it, webhook signatures cannot be verified and anyone could post
            # forged inbound messages (§19).
            if not self.whatsapp_app_secret:
                missing.append("WHATSAPP_APP_SECRET")
            if not self.whatsapp_business_account_id:
                missing.append("WHATSAPP_BUSINESS_ACCOUNT_ID")

        if self.calendar_provider is CalendarProviderName.GOOGLE:
            if not self.google_calendar_id:
                missing.append("GOOGLE_CALENDAR_ID")
            if not self.google_secret_manager_project:
                missing.append("GOOGLE_SECRET_MANAGER_PROJECT")
            if not self.google_calendar_token_secret_name:
                missing.append("GOOGLE_CALENDAR_TOKEN_SECRET_NAME")
            if not self.google_oauth_client_id:
                missing.append("GOOGLE_OAUTH_CLIENT_ID")
            if not self.google_oauth_client_secret:
                missing.append("GOOGLE_OAUTH_CLIENT_SECRET")

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
        if not self.rate_limit_enabled:
            # §19 requires rate limits. The flag exists for tests that need to make many
            # attempts; production must not be able to start without them.
            raise ValueError("RATE_LIMIT_ENABLED cannot be disabled in production (§19)")
        if self.audience_column_schema == PLACEHOLDER_AUDIENCE_COLUMN_SCHEMA:
            raise ValueError(
                "AUDIENCE_COLUMN_SCHEMA is still the development placeholder. "
                "Set the client's confirmed Excel column schema before production "
                "([D4]/[D20])."
            )
        return self

    #: Cloud Tasks rejects a task larger than 1 MB. Base64 costs a third, and the task
    #: also carries its own JSON envelope.
    CLOUD_TASKS_SAFE_UPLOAD_BYTES: ClassVar[int] = 700_000

    @model_validator(mode="after")
    def _validate_upload_fits_the_queue(self) -> Settings:
        """Refuse an upload limit the selected queue cannot actually carry.

        Only applies to Cloud Tasks: the in-memory queue used in development and tests
        has no such limit, and capping it there would make local work harder for no
        reason.
        """
        if self.queue_provider is not QueueProviderName.CLOUD_TASKS:
            return self
        if self.knowledge_max_file_bytes > self.CLOUD_TASKS_SAFE_UPLOAD_BYTES:
            raise ValueError(
                f"KNOWLEDGE_MAX_FILE_BYTES={self.knowledge_max_file_bytes} exceeds what "
                f"a Cloud Tasks payload can carry ({self.CLOUD_TASKS_SAFE_UPLOAD_BYTES} "
                "bytes after base64). V1 sends document bytes inside the task; a larger "
                "limit needs object storage with the task carrying only a reference."
            )
        return self

    # ---------------------------------------------------------------- helpers

    def audience_schema(self) -> AudienceColumnSchema:
        """Parse the configured column schema.

        Imported locally to keep `app.core` free of a dependency on `app.domain`,
        which would otherwise invert the layering.
        """
        import json

        from app.domain.audience import AudienceColumnSchema

        return AudienceColumnSchema(columns=json.loads(self.audience_column_schema))

    @property
    def audience_schema_is_placeholder(self) -> bool:
        """True while the development placeholder is still in force ([D20])."""
        return self.audience_column_schema == PLACEHOLDER_AUDIENCE_COLUMN_SCHEMA

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
