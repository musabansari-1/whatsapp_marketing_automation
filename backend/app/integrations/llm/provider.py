"""LLM provider interface (specification §9).

    LLMProvider
     ├── OpenRouterLLMProvider  (Phase 10)
     └── FakeLLMProvider

The provider is a transport. It does not decide business outcomes: per [D9]/[D18] the
model extracts structured information and the backend applies qualification rules,
and per §9 the model may only request the six authorized tools.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class AuthorizedTool(StrEnum):
    """The only tools the AI may request (§9). Implemented in Phase 10."""

    GET_LEAD = "get_lead"
    UPDATE_LEAD = "update_lead"
    GET_CONVERSATION = "get_conversation"
    GET_AVAILABLE_SLOTS = "get_available_slots"
    BOOK_DEMO = "book_demo"
    SEND_WHATSAPP_MESSAGE = "send_whatsapp_message"


@dataclass(frozen=True, slots=True)
class Message:
    role: Role
    content: str
    tool_call_id: str | None = None


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A tool the model asked to run.

    `name` is validated against `AuthorizedTool` by the caller before dispatch; an
    unrecognized name is refused rather than executed.
    """

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: AuthorizedTool
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True, slots=True)
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True, slots=True)
class LLMCompletion:
    """A single model response.

    `model` records which model actually answered, for the `ai_runs` audit trail and
    for the Phase 10 benchmarking that [D17] defers the model choice to.
    """

    text: str
    model: str
    tool_calls: tuple[ToolCall, ...] = ()
    usage: TokenUsage = field(default_factory=TokenUsage)
    finish_reason: str = "stop"


@dataclass(frozen=True, slots=True)
class CompletionRequest:
    messages: tuple[Message, ...]
    tools: tuple[ToolSpec, ...] = ()
    temperature: float | None = None
    max_tokens: int | None = None


class LLMProvider(ABC):
    name: str

    @property
    @abstractmethod
    def model(self) -> str:
        """The configured model identifier. Never hard-coded (CLAUDE.md, [D17])."""

    @abstractmethod
    async def complete(self, request: CompletionRequest) -> LLMCompletion:
        """Produce one completion, optionally requesting authorized tools."""
