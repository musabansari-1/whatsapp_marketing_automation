"""In-memory WhatsApp provider for automated tests and local development.

CLAUDE.md: automated tests must never send real WhatsApp messages. This provider
performs no network I/O whatsoever, records everything it was asked to send so tests
can assert on it, and can be told to fail so retry paths are testable.
"""

from __future__ import annotations

import itertools

from app.core.errors import NonRetryableProviderError, RetryableProviderError
from app.integrations.whatsapp.provider import (
    ApprovedTemplate,
    MessageStatus,
    OutboundTemplateMessage,
    OutboundTextMessage,
    SentMessage,
    WhatsAppProvider,
)


class FakeWhatsAppProvider(WhatsAppProvider):
    name = "fake"

    def __init__(
        self,
        *,
        approved_templates: list[ApprovedTemplate] | None = None,
        fail_with: Exception | None = None,
    ) -> None:
        self.sent_templates: list[OutboundTemplateMessage] = []
        self.sent_texts: list[OutboundTextMessage] = []
        self.approved_templates = list(approved_templates or [])
        self.fail_with = fail_with
        self._counter = itertools.count(1)
        # Maps idempotency key -> already-issued result, so a repeated send is
        # collapsed exactly as a well-behaved real provider would allow (§20).
        self._by_idempotency_key: dict[str, SentMessage] = {}

    # ---------------------------------------------------------------- helpers

    def _next_id(self) -> str:
        return f"fake-wamid-{next(self._counter)}"

    def _maybe_fail(self) -> None:
        if self.fail_with is not None:
            raise self.fail_with

    def _issue(self, idempotency_key: str | None) -> tuple[SentMessage, bool]:
        """Return (result, was_replayed)."""
        if idempotency_key and idempotency_key in self._by_idempotency_key:
            return self._by_idempotency_key[idempotency_key], True
        result = SentMessage(provider_message_id=self._next_id(), status=MessageStatus.SENT)
        if idempotency_key:
            self._by_idempotency_key[idempotency_key] = result
        return result, False

    # ---------------------------------------------------------------- interface

    async def send_template_message(self, message: OutboundTemplateMessage) -> SentMessage:
        self._maybe_fail()
        result, replayed = self._issue(message.idempotency_key)
        if not replayed:
            self.sent_templates.append(message)
        return result

    async def send_text_message(self, message: OutboundTextMessage) -> SentMessage:
        self._maybe_fail()
        result, replayed = self._issue(message.idempotency_key)
        if not replayed:
            self.sent_texts.append(message)
        return result

    async def fetch_approved_templates(self) -> list[ApprovedTemplate]:
        self._maybe_fail()
        return list(self.approved_templates)

    # ---------------------------------------------------------------- test helpers

    @property
    def total_sent(self) -> int:
        return len(self.sent_templates) + len(self.sent_texts)

    def recipients(self) -> list[str]:
        return [m.to_e164 for m in self.sent_templates] + [m.to_e164 for m in self.sent_texts]

    def reset(self) -> None:
        self.sent_templates.clear()
        self.sent_texts.clear()
        self._by_idempotency_key.clear()

    def fail_retryably(self, message: str = "simulated WhatsApp outage") -> None:
        self.fail_with = RetryableProviderError(message, provider=self.name)

    def fail_permanently(self, message: str = "simulated invalid recipient") -> None:
        self.fail_with = NonRetryableProviderError(message, provider=self.name)

    def stop_failing(self) -> None:
        self.fail_with = None
