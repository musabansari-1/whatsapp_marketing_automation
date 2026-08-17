"""Deterministic LLM provider for automated tests.

Scripted rather than generative: tests queue the exact completions they expect the
conversation to produce, so Phase 9/10 behaviour is asserted against known model
output instead of a live model's variability.
"""

from __future__ import annotations

from collections import deque

from app.core.errors import NonRetryableProviderError, RetryableProviderError
from app.integrations.llm.provider import (
    CompletionRequest,
    LLMCompletion,
    LLMProvider,
    ToolCall,
)

FAKE_MODEL = "fake/deterministic-1"


class FakeLLMProvider(LLMProvider):
    name = "fake"

    def __init__(
        self,
        *,
        scripted: list[LLMCompletion] | None = None,
        default_text: str = "This is a fake completion.",
        fail_with: Exception | None = None,
    ) -> None:
        self._scripted: deque[LLMCompletion] = deque(scripted or [])
        self.default_text = default_text
        self.fail_with = fail_with
        self.requests: list[CompletionRequest] = []

    @property
    def model(self) -> str:
        return FAKE_MODEL

    async def complete(self, request: CompletionRequest) -> LLMCompletion:
        if self.fail_with is not None:
            raise self.fail_with
        self.requests.append(request)
        if self._scripted:
            return self._scripted.popleft()
        return LLMCompletion(text=self.default_text, model=self.model)

    # ---------------------------------------------------------------- test helpers

    def queue_text(self, text: str) -> None:
        self._scripted.append(LLMCompletion(text=text, model=self.model))

    def queue_tool_call(self, name: str, arguments: dict[str, object]) -> None:
        """Queue a tool request.

        `name` is intentionally a plain string so tests can also script an
        unauthorized tool name and assert the caller refuses it.
        """
        call = ToolCall(id=f"call-{len(self.requests) + 1}", name=name, arguments=dict(arguments))
        self._scripted.append(
            LLMCompletion(text="", model=self.model, tool_calls=(call,), finish_reason="tool_calls")
        )

    def queue_completion(self, completion: LLMCompletion) -> None:
        self._scripted.append(completion)

    @property
    def call_count(self) -> int:
        return len(self.requests)

    @property
    def pending(self) -> int:
        return len(self._scripted)

    def last_request(self) -> CompletionRequest | None:
        return self.requests[-1] if self.requests else None

    def prompt_texts(self) -> list[str]:
        """Every message body sent to the model, for prompt-injection assertions."""
        return [m.content for request in self.requests for m in request.messages]

    def reset(self) -> None:
        self.requests.clear()
        self._scripted.clear()

    def fail_retryably(self, message: str = "simulated LLM rate limit") -> None:
        self.fail_with = RetryableProviderError(message, provider=self.name)

    def fail_permanently(self, message: str = "simulated LLM auth failure") -> None:
        self.fail_with = NonRetryableProviderError(message, provider=self.name)

    def stop_failing(self) -> None:
        self.fail_with = None
