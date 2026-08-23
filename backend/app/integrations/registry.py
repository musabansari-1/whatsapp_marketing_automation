"""Provider selection.

This module is the single place that maps configuration to concrete providers, which
is what lets CLAUDE.md's rule hold: no layer above `app.integrations` names a provider.
Callers depend on the interfaces and receive whatever configuration selected.

Real providers arrive in their own phases. Selecting one before it exists raises
`ProviderNotImplementedError` naming the phase, so a misconfiguration fails loudly at
startup instead of silently degrading.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import (
    CalendarProviderName,
    CRMProviderName,
    EmbeddingProviderName,
    LLMProviderName,
    QueueProviderName,
    Settings,
    WhatsAppProviderName,
    get_settings,
)
from app.core.errors import ConfigurationError, ProviderNotImplementedError
from app.integrations.calendar import CalendarProvider, FakeCalendarProvider
from app.integrations.calendar.google import GoogleCalendarProvider
from app.integrations.crm import CRMProvider, FakeCRMProvider
from app.integrations.embedding import EmbeddingProvider, FakeEmbeddingProvider
from app.integrations.embedding.local_sentence_transformer import (
    LocalSentenceTransformerProvider,
)
from app.integrations.llm import FakeLLMProvider, LLMProvider
from app.integrations.llm.openrouter import OpenRouterLLMProvider
from app.integrations.queue import InMemoryQueueProvider, QueueProvider
from app.integrations.queue.cloud_tasks import CloudTasksProvider
from app.integrations.secrets import (
    GoogleSecretManagerStore,
    InMemorySecretStore,
    SecretStore,
)
from app.integrations.whatsapp import FakeWhatsAppProvider, WhatsAppProvider
from app.integrations.whatsapp.meta import MetaWhatsAppProvider


@dataclass(frozen=True, slots=True)
class Providers:
    """The six integrations, resolved from configuration."""

    whatsapp: WhatsAppProvider
    llm: LLMProvider
    embedding: EmbeddingProvider
    calendar: CalendarProvider
    crm: CRMProvider
    queue: QueueProvider


def build_whatsapp_provider(settings: Settings) -> WhatsAppProvider:
    match settings.whatsapp_provider:
        case WhatsAppProviderName.FAKE:
            return FakeWhatsAppProvider()
        case WhatsAppProviderName.META:
            # Configuration validation already guarantees these are present when META
            # is selected. Re-checked rather than asserted because `python -O` strips
            # asserts, which would turn a misconfiguration into a confusing None.
            if (
                settings.whatsapp_access_token is None
                or settings.whatsapp_phone_number_id is None
                or settings.whatsapp_business_account_id is None
            ):
                raise ConfigurationError(
                    "The Meta WhatsApp provider is selected but its credentials are missing"
                )
            return MetaWhatsAppProvider(
                access_token=settings.whatsapp_access_token.get_secret_value(),
                phone_number_id=settings.whatsapp_phone_number_id,
                business_account_id=settings.whatsapp_business_account_id,
                api_version=settings.whatsapp_api_version,
                base_url=settings.whatsapp_api_base_url,
                timeout_seconds=settings.whatsapp_timeout_seconds,
            )


def build_llm_provider(settings: Settings) -> LLMProvider:
    match settings.llm_provider:
        case LLMProviderName.FAKE:
            return FakeLLMProvider()
        case LLMProviderName.OPENROUTER:
            # Both are guaranteed present: configuration refuses to start with
            # OPENROUTER_MODEL unset, because [D17] forbids a baked-in default.
            return OpenRouterLLMProvider(
                api_key=settings.openrouter_api_key.get_secret_value()
                if settings.openrouter_api_key
                else "",
                model=settings.openrouter_model or "",
                base_url=settings.openrouter_base_url,
                timeout_seconds=settings.openrouter_timeout_seconds,
            )


def build_embedding_provider(settings: Settings) -> EmbeddingProvider:
    match settings.embedding_provider:
        case EmbeddingProviderName.FAKE:
            # Dimension mirrors configuration so fake vectors stay compatible with
            # the pgvector column Phase 2 creates.
            return FakeEmbeddingProvider(dimension=settings.embedding_dimension)
        case EmbeddingProviderName.LOCAL_SENTENCE_TRANSFORMER:
            # The model is loaded lazily, so building this costs nothing until
            # something actually embeds ([D1]).
            return LocalSentenceTransformerProvider(
                model_name=settings.embedding_model,
                dimension=settings.embedding_dimension,
                batch_size=settings.embedding_batch_size,
            )


def build_calendar_provider(settings: Settings) -> CalendarProvider:
    match settings.calendar_provider:
        case CalendarProviderName.FAKE:
            return FakeCalendarProvider()
        case CalendarProviderName.GOOGLE:
            # Every value below is guaranteed present: configuration refuses to start
            # when the Google calendar is selected without them.
            return GoogleCalendarProvider(
                calendar_id=settings.google_calendar_id or "",
                client_id=settings.google_oauth_client_id or "",
                client_secret=(
                    settings.google_oauth_client_secret.get_secret_value()
                    if settings.google_oauth_client_secret
                    else ""
                ),
                # [D19]: the refresh token comes from Secret Manager, never from
                # configuration and never from the database.
                secret_store=build_secret_store(settings),
                refresh_token_secret_name=settings.google_calendar_token_secret_name or "",
                timezone=settings.demo_timezone,
                timeout_seconds=settings.google_calendar_timeout_seconds,
            )


def build_secret_store(settings: Settings) -> SecretStore:
    """Where secrets are read from ([D19]).

    Google Secret Manager only when the real calendar is selected: [D19] is explicit
    that local development and tests must not require it.
    """
    if settings.calendar_provider is CalendarProviderName.GOOGLE:
        return GoogleSecretManagerStore(project_id=settings.google_secret_manager_project or "")
    return InMemorySecretStore()


def build_crm_provider(settings: Settings) -> CRMProvider:
    match settings.crm_provider:
        case CRMProviderName.FAKE:
            return FakeCRMProvider()
        case CRMProviderName.HUBSPOT:
            raise ProviderNotImplementedError("hubspot", "Phase 12 (HubSpot)")


def build_queue_provider(settings: Settings) -> QueueProvider:
    match settings.queue_provider:
        case QueueProviderName.INLINE:
            return InMemoryQueueProvider()
        case QueueProviderName.CLOUD_TASKS:
            # Configuration validation guarantees these when CLOUD_TASKS is selected;
            # re-checked because `python -O` strips asserts.
            if (
                settings.google_cloud_project is None
                or settings.google_cloud_tasks_queue is None
                or settings.worker_base_url is None
            ):
                raise ConfigurationError(
                    "The Cloud Tasks queue is selected but its configuration is missing"
                )
            return CloudTasksProvider(
                project=settings.google_cloud_project,
                location=settings.google_cloud_location,
                queue=settings.google_cloud_tasks_queue,
                worker_base_url=settings.worker_base_url,
                service_account_email=settings.worker_service_account_email,
                dispatch_rate_per_second=settings.dispatch_rate_per_second,
            )


def build_providers(settings: Settings | None = None) -> Providers:
    settings = settings or get_settings()
    return Providers(
        whatsapp=build_whatsapp_provider(settings),
        llm=build_llm_provider(settings),
        embedding=build_embedding_provider(settings),
        calendar=build_calendar_provider(settings),
        crm=build_crm_provider(settings),
        queue=build_queue_provider(settings),
    )
