"""OpenRouter LLM provider (specification §9, §20, [D10], [D17]).

The real implementation behind `LLMProvider`. Nothing above `app.integrations` imports
this module; it is selected by configuration.

**No model is hard-coded.** [D17] leaves the choice to Phase 10 benchmarking, so the
model arrives through the constructor from `OPENROUTER_MODEL`, which has no default and
fails startup when unset. A provider that quietly picked a model would make that
decision by accident.

Failure classification (§20) is the load-bearing part, and OpenRouter makes it harder
than Meta does: it proxies many upstream vendors, so a 502 can mean "OpenRouter is
down" or "the chosen model's vendor is down". Both are transient. A 400 about an
unknown model will never succeed, however many times it is retried.

API keys are never logged (§19).
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.core.errors import NonRetryableProviderError, RetryableProviderError
from app.core.logging import get_logger
from app.integrations.llm.provider import (
    CompletionRequest,
    LLMCompletion,
    LLMProvider,
    Message,
    Role,
    TokenUsage,
    ToolCall,
    ToolSpec,
)

logger = get_logger(__name__)

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"

# OpenRouter error codes that will not succeed on retry.
# 400 covers a malformed request; 401/403 rejected credentials; 404 an unknown model.
# 402 is out of credit — retrying spends nothing and fixes nothing.
_NON_RETRYABLE_STATUSES = frozenset({400, 401, 402, 403, 404, 422})


class OpenRouterLLMProvider(LLMProvider):
    """Talks to the OpenRouter chat completions API over HTTPS."""

    name = "openrouter"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = 60.0,
        app_url: str | None = None,
        app_title: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not model:
            # Belt and braces: config already fails startup without it ([D17]), but a
            # provider constructed directly must not invent one either.
            raise ValueError("OpenRouter requires an explicit model; there is no default")

        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        # Generous next to the WhatsApp timeout: a model generating a long reply
        # legitimately takes tens of seconds, and cutting it off wastes the tokens
        # already spent.
        self._timeout = timeout_seconds
        self._app_url = app_url
        self._app_title = app_title
        # Injectable so tests supply a mock transport and never touch the network.
        self._client = client

    @property
    def model(self) -> str:
        return self._model

    async def complete(self, request: CompletionRequest) -> LLMCompletion:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [_encode_message(message) for message in request.messages],
        }
        if request.tools:
            payload["tools"] = [_encode_tool(tool) for tool in request.tools]
            # "auto" rather than forcing a call: most turns are a plain reply, and
            # forcing a tool would make the model invent one.
            payload["tool_choice"] = "auto"
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens

        body = await self._request("POST", f"{self._base_url}/chat/completions", json=payload)
        return self._parse_completion(body)

    # ---------------------------------------------------------------- internals

    def _parse_completion(self, body: dict[str, Any]) -> LLMCompletion:
        choices = body.get("choices") or []
        if not choices:
            raise NonRetryableProviderError("OpenRouter returned no choices", provider=self.name)

        choice = choices[0]
        message = choice.get("message") or {}
        usage = body.get("usage") or {}

        # The model that actually answered, which can differ from the one requested:
        # OpenRouter falls back across providers. Recorded per run in `ai_runs` for
        # exactly this reason.
        model = body.get("model") or self._model

        return LLMCompletion(
            text=message.get("content") or "",
            model=model,
            tool_calls=tuple(_parse_tool_calls(message.get("tool_calls") or [])),
            usage=TokenUsage(
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
            ),
            finish_reason=choice.get("finish_reason") or "stop",
        )

    async def _request(
        self, method: str, url: str, *, json: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        # OpenRouter attributes traffic with these; both are optional and neither
        # carries a secret.
        if self._app_url:
            headers["HTTP-Referer"] = self._app_url
        if self._app_title:
            headers["X-Title"] = self._app_title

        try:
            if self._client is not None:
                response = await self._client.request(
                    method, url, json=json, headers=headers, timeout=self._timeout
                )
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.request(method, url, json=json, headers=headers)
        except httpx.TimeoutException as exc:
            # A timeout says nothing about whether the request was processed. It is
            # retryable, and the caller's idempotency key is what prevents the
            # customer receiving two replies (§20).
            raise RetryableProviderError(f"Request timed out: {exc}", provider=self.name) from exc
        except httpx.HTTPError as exc:
            raise RetryableProviderError(f"Network failure: {exc}", provider=self.name) from exc

        if response.is_success:
            try:
                parsed: dict[str, Any] = response.json()
            except ValueError as exc:
                raise NonRetryableProviderError(
                    "OpenRouter returned a success status with an unreadable body",
                    provider=self.name,
                ) from exc

            # A 200 can still carry an error: OpenRouter reports some upstream
            # failures in the body rather than the status.
            if "error" in parsed and not parsed.get("choices"):
                raise self._classify_body_error(parsed["error"])
            return parsed

        raise self._classify_error(response)

    def _classify_body_error(self, error: Any) -> Exception:
        code = error.get("code") if isinstance(error, dict) else None
        message = (error.get("message", str(error)) if isinstance(error, dict) else str(error))[
            :500
        ]

        if isinstance(code, int) and code in _NON_RETRYABLE_STATUSES:
            return NonRetryableProviderError(f"[{code}] {message}", provider=self.name)
        # Unknown codes are treated as transient: the retry budget is finite and
        # bounded, whereas giving up on a recoverable failure loses the customer's
        # reply entirely.
        return RetryableProviderError(f"[{code}] {message}", provider=self.name)

    def _classify_error(self, response: httpx.Response) -> Exception:
        """Turn an error response into the §20 retryable/non-retryable split."""
        message = response.text[:500]
        try:
            error = response.json().get("error", {})
            message = error.get("message", message)
        except ValueError:
            pass

        if response.status_code == 429:
            # Respected and retried, as [D27] requires: the request was never
            # processed, so there is nothing to duplicate.
            return RetryableProviderError(f"Rate limited: {message}", provider=self.name)
        if response.status_code in _NON_RETRYABLE_STATUSES:
            return NonRetryableProviderError(
                f"[{response.status_code}] {message}", provider=self.name
            )
        if response.status_code >= 500:
            # OpenRouter proxies many vendors, so this covers both its own outage and
            # the chosen model's. Both pass.
            return RetryableProviderError(
                f"Upstream error {response.status_code}: {message}", provider=self.name
            )

        return RetryableProviderError(
            f"Unexpected status {response.status_code}: {message}", provider=self.name
        )


# ---------------------------------------------------------------- encoding


def _encode_message(message: Message) -> dict[str, Any]:
    encoded: dict[str, Any] = {"role": message.role.value, "content": message.content}
    if message.role is Role.TOOL and message.tool_call_id:
        encoded["tool_call_id"] = message.tool_call_id
    return encoded


def _encode_tool(tool: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name.value,
            "description": tool.description,
            "parameters": tool.parameters,
        },
    }


def _parse_tool_calls(raw_calls: list[Any]) -> list[ToolCall]:
    """Read the model's tool requests.

    Arguments arrive as a JSON *string* and the model can emit malformed JSON. A
    broken argument blob is surfaced as an empty mapping rather than raising: the
    caller validates arguments anyway, and losing the whole turn because one field was
    mis-serialized would be worse than refusing one tool call.
    """
    calls: list[ToolCall] = []

    for raw in raw_calls:
        function = raw.get("function") or {}
        name = function.get("name")
        if not name:
            continue

        arguments: dict[str, Any] = {}
        raw_arguments = function.get("arguments")
        if isinstance(raw_arguments, dict):
            arguments = raw_arguments
        elif isinstance(raw_arguments, str) and raw_arguments.strip():
            try:
                parsed = json.loads(raw_arguments)
                if isinstance(parsed, dict):
                    arguments = parsed
                else:
                    logger.warning("llm_tool_arguments_not_an_object", tool=name)
            except ValueError:
                logger.warning("llm_tool_arguments_unparseable", tool=name)

        calls.append(
            ToolCall(id=raw.get("id") or f"call-{len(calls)}", name=name, arguments=arguments)
        )

    return calls


__all__ = ["OpenRouterLLMProvider"]
