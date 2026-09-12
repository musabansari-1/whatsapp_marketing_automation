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

    WhatsApp and the calendar are absent on purpose. Each product has its own number
    ([D40]) and its own calendar ([D43]), so there is nothing to build without knowing
    whose — and a slot here for a platform-wide one would be exactly the fallback those
    decisions rule out. Use `build_whatsapp_provider` and `build_calendar_provider` with
    the product.
    """

    llm: LLMProvider
    embedding: EmbeddingProvider
    crm: CRMProvider
    queue: QueueProvider


class WhatsAppNotConfiguredError(ConfigurationError):
    """This product has no WhatsApp number or business account, so nothing is sent from it.

    Raised rather than defaulted. Until Phase 27 a product without a number sent from the
    platform-wide `WHATSAPP_PHONE_NUMBER_ID` — which, once three products exist, is another
    product's number: a SustainX customer would have been messaged from Academicae's.
    """


def build_whatsapp_provider(settings: Settings, *, product: Product) -> WhatsAppProvider:
    """The WhatsApp provider for one product (§28, [D40]).

    `product` is required. It supplies the **phone number** a message goes out from and the
    business account that number belongs to. One Meta App serves every product, so the
    access token is shared; nothing else is, and there is no platform-wide number.

    Nothing accepts a `phone_number_id` argument, so no code path can send a SustainX
    campaign through Academicae's number — the domain object picks the product, and the
    product picks the number.

    Checked before the provider is selected, so the fake refuses exactly as the real one
    does and the rule is exercised by the test suite rather than only in production.
    """
    if not product.whatsapp_phone_number_id or not product.whatsapp_business_account_id:
        raise WhatsAppNotConfiguredError(
            f"Product {product.slug!r} has no WhatsApp phone number ID and business account "
            "configured, so nothing is sent from it"
        )

    match settings.whatsapp_provider:
        case WhatsAppProviderName.FAKE:
            return FakeWhatsAppProvider()
        case WhatsAppProviderName.META:
            return MetaWhatsAppProvider(
                access_token=_whatsapp_access_token(settings),
                phone_number_id=product.whatsapp_phone_number_id,
                business_account_id=product.whatsapp_business_account_id,
                api_version=settings.whatsapp_api_version,
                base_url=settings.whatsapp_api_base_url,
                timeout_seconds=settings.whatsapp_timeout_seconds,
            )


def build_whatsapp_template_source(
    settings: Settings, *, business_account_id: str
) -> WhatsAppProvider:
    """A provider that reads one WhatsApp Business Account's templates ([D6], [D57]).

    Template sync is account-level: a template belongs to a WABA, not to a product, and
    which product owns it is an administrator's decision afterwards. So this takes an
    account and no product, and the provider it returns has no number to send from.
    """
    match settings.whatsapp_provider:
        case WhatsAppProviderName.FAKE:
            return FakeWhatsAppProvider()
        case WhatsAppProviderName.META:
            return MetaWhatsAppProvider(
                access_token=_whatsapp_access_token(settings),
                phone_number_id=None,
                business_account_id=business_account_id,
                api_version=settings.whatsapp_api_version,
                base_url=settings.whatsapp_api_base_url,
                timeout_seconds=settings.whatsapp_timeout_seconds,
            )


def _whatsapp_access_token(settings: Settings) -> str:
    # Configuration validation already guarantees the token when META is selected.
    # Re-checked rather than asserted because `python -O` strips asserts, which would turn a
    # misconfiguration into a confusing None.
    if settings.whatsapp_access_token is None:
        raise ConfigurationError(
            "The Meta WhatsApp provider is selected but its access token is missing"
        )
    return settings.whatsapp_access_token.get_secret_value()


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
        llm=build_llm_provider(settings),
        embedding=build_embedding_provider(settings),
        crm=build_crm_provider(settings),
        queue=build_queue_provider(settings),
    )
