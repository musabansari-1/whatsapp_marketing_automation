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
from app.core.errors import ConfigurationError
from app.domain.slots import resolve_slot_policy
from app.integrations.calendar import CalendarProvider, FakeCalendarProvider
from app.integrations.calendar.google import GoogleCalendarProvider
from app.integrations.crm import CRMProvider, FakeCRMProvider
from app.integrations.crm.hubspot import HubSpotProvider
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
from app.persistence.models import Product


@dataclass(frozen=True, slots=True)
class Providers:
    """The integrations that can be built without a product, from configuration.

    The calendar is absent on purpose. Each product has its own ([D43]), so there is no
    calendar to build without knowing whose — and a slot here for a platform-wide one would
    be exactly the fallback [D43] rules out. Use `build_calendar_provider` with the product.
    """

    whatsapp: WhatsAppProvider
    llm: LLMProvider
    embedding: EmbeddingProvider
    crm: CRMProvider
    queue: QueueProvider


def build_whatsapp_provider(
    settings: Settings, *, product: Product | None = None
) -> WhatsAppProvider:
    """The WhatsApp provider for one product (§28, [D40]).

    `product` supplies the **phone number** a message goes out from, which is what
    identifies a product: one Meta App and one shared WABA serve all of them, so the
    WABA identifies nothing and the credential is shared unless a product overrides it.

    Keeping the number here rather than in the caller is deliberate. Nothing accepts a
    `phone_number_id` argument, so no code path can send a SustainX campaign through
    Academicae's number — the domain object picks the product, and the product picks the
    number.

    `product=None` uses the platform-wide configuration, which is what the fake provider
    and the not-yet-product-aware paths need. It cannot be used to reach a real number
    for a product that has not configured one: the values simply come from settings, as
    they always did.
    """
    match settings.whatsapp_provider:
        case WhatsAppProviderName.FAKE:
            return FakeWhatsAppProvider()
        case WhatsAppProviderName.META:
            phone_number_id = (
                product.whatsapp_phone_number_id
                if product is not None and product.whatsapp_phone_number_id
                else settings.whatsapp_phone_number_id
            )
            business_account_id = (
                product.whatsapp_business_account_id
                if product is not None and product.whatsapp_business_account_id
                else settings.whatsapp_business_account_id
            )

            # Configuration validation already guarantees these are present when META
            # is selected. Re-checked rather than asserted because `python -O` strips
            # asserts, which would turn a misconfiguration into a confusing None.
            if (
                settings.whatsapp_access_token is None
                or phone_number_id is None
                or business_account_id is None
            ):
                raise ConfigurationError(
                    "The Meta WhatsApp provider is selected but its credentials are missing"
                )
            return MetaWhatsAppProvider(
                access_token=settings.whatsapp_access_token.get_secret_value(),
                phone_number_id=phone_number_id,
                business_account_id=business_account_id,
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


class CalendarNotConfiguredError(ConfigurationError):
    """This product has no calendar, so nothing may be booked for it ([D43]).

    Raised rather than defaulted. The alternatives are all worse: another product's
    calendar would offer that product's free time and book into its diary, and a
    platform-wide calendar would be a shared sales calendar by another name.
    """


def build_calendar_provider(settings: Settings, *, product: Product) -> CalendarProvider:
    """The calendar for one product ([D43]).

    `product` is required. Nothing builds a calendar without knowing whose it is, and no
    caller supplies a calendar ID — the product row does, the same way it supplies the
    WhatsApp number a message leaves from.

    What varies by product: the calendar ID, optionally the refresh-token *secret name*
    (for a calendar in a different Google account), and the timezone, from its resolved
    slot policy. What is shared: the OAuth client and the Secret Manager project, because
    one operations account holds access to every product's calendar.

    Checked before the provider is selected, so the fake refuses exactly as the real one
    does and the rule is exercised by the test suite rather than only in production.
    """
    if not product.google_calendar_id:
        raise CalendarNotConfiguredError(
            f"Product {product.slug!r} has no calendar configured, so bookings are refused for it"
        )

    match settings.calendar_provider:
        case CalendarProviderName.FAKE:
            return FakeCalendarProvider(calendar_id=product.google_calendar_id)
        case CalendarProviderName.GOOGLE:
            # The shared values are guaranteed present: configuration refuses to start
            # when the Google calendar is selected without them.
            return GoogleCalendarProvider(
                calendar_id=product.google_calendar_id,
                client_id=settings.google_oauth_client_id or "",
                client_secret=(
                    settings.google_oauth_client_secret.get_secret_value()
                    if settings.google_oauth_client_secret
                    else ""
                ),
                # [D19]: the refresh token comes from Secret Manager, never from
                # configuration and never from the database. Only its name may vary.
                secret_store=build_secret_store(settings),
                refresh_token_secret_name=(
                    product.google_calendar_token_secret_name
                    or settings.google_calendar_token_secret_name
                    or ""
                ),
                timezone=resolve_slot_policy(settings, product).timezone,
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
            # Guaranteed present: configuration refuses to start without it.
            return HubSpotProvider(
                access_token=(
                    settings.hubspot_access_token.get_secret_value()
                    if settings.hubspot_access_token
                    else ""
                ),
                base_url=settings.hubspot_base_url,
                timeout_seconds=settings.hubspot_timeout_seconds,
            )


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
        crm=build_crm_provider(settings),
        queue=build_queue_provider(settings),
    )
