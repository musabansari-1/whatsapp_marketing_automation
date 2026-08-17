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
from app.core.errors import ProviderNotImplementedError
from app.integrations.calendar import CalendarProvider, FakeCalendarProvider
from app.integrations.crm import CRMProvider, FakeCRMProvider
from app.integrations.embedding import EmbeddingProvider, FakeEmbeddingProvider
from app.integrations.llm import FakeLLMProvider, LLMProvider
from app.integrations.queue import InMemoryQueueProvider, QueueProvider
from app.integrations.whatsapp import FakeWhatsAppProvider, WhatsAppProvider


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
            raise ProviderNotImplementedError("meta", "Phase 6 (WhatsApp)")


def build_llm_provider(settings: Settings) -> LLMProvider:
    match settings.llm_provider:
        case LLMProviderName.FAKE:
            return FakeLLMProvider()
        case LLMProviderName.OPENROUTER:
            raise ProviderNotImplementedError("openrouter", "Phase 10 (AI agent)")


def build_embedding_provider(settings: Settings) -> EmbeddingProvider:
    match settings.embedding_provider:
        case EmbeddingProviderName.FAKE:
            # Dimension mirrors configuration so fake vectors stay compatible with
            # the pgvector column Phase 2 creates.
            return FakeEmbeddingProvider(dimension=settings.embedding_dimension)
        case EmbeddingProviderName.LOCAL_SENTENCE_TRANSFORMER:
            raise ProviderNotImplementedError(
                "local_sentence_transformer", "Phase 8 (RAG knowledge base)"
            )


def build_calendar_provider(settings: Settings) -> CalendarProvider:
    match settings.calendar_provider:
        case CalendarProviderName.FAKE:
            return FakeCalendarProvider()
        case CalendarProviderName.GOOGLE:
            raise ProviderNotImplementedError("google", "Phase 11 (Calendar and booking)")


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
            raise ProviderNotImplementedError("cloud_tasks", "Phase 7 (Cloud Tasks and worker)")


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
