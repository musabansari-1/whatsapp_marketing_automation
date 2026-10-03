"""OpenRouter provider tests (§9, §19, §20, [D10], [D17]).

Every request is served by an in-memory mock transport: no socket is opened, no token
is spent, and no model is called.

The failure classification is the focus, and OpenRouter makes it harder than Meta does.
It proxies many upstream vendors, so a 502 can mean OpenRouter is down or that the
chosen model's vendor is — both transient. A 400 about an unknown model never will be.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.core.errors import NonRetryableProviderError, RetryableProviderError
from app.integrations.llm import AuthorizedTool, CompletionRequest, Message, Role, ToolSpec
from app.integrations.llm.openrouter import OpenRouterLLMProvider

MODEL = "vendor/some-model"


def make_provider(handler: Any, *, model: str = MODEL) -> OpenRouterLLMProvider:
    return OpenRouterLLMProvider(
        api_key="secret-key",  # noqa: S106 - never leaves the mock
        model=model,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def completion_body(
    text: str = "Hello there.",
    *,
    tool_calls: list[dict[str, Any]] | None = None,
    model: str = MODEL,
    finish_reason: str = "stop",
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": text}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls

    return {
        "model": model,
        "choices": [{"message": message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 7},
    }


def responds(body: dict[str, Any], status_code: int = 200) -> Any:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=body)

    return handler


def a_request(text: str = "Hi") -> CompletionRequest:
    return CompletionRequest(messages=(Message(role=Role.USER, content=text),))


class TestModelIsNeverInvented:
    def test_the_configured_model_is_used(self) -> None:
        assert make_provider(responds(completion_body())).model == MODEL

    def test_construction_without_a_model_is_refused(self) -> None:
        """[D17] forbids a baked-in default. Configuration already fails startup, and
        this is the backstop for a provider constructed directly."""
        with pytest.raises(ValueError, match="no default"):
            OpenRouterLLMProvider(api_key="k", model="")  # noqa: S106

    @pytest.mark.asyncio
    async def test_the_model_is_sent_in_the_request(self) -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            seen.update(json.loads(request.content))
            return httpx.Response(200, json=completion_body())

        await make_provider(handler).complete(a_request())
        assert seen["model"] == MODEL

    @pytest.mark.asyncio
    async def test_the_answering_model_is_reported_not_assumed(self) -> None:
        """OpenRouter falls back across providers, so the model that answered can
        differ from the one asked for. `ai_runs` records what actually ran."""
        provider = make_provider(responds(completion_body(model="vendor/fallback-model")))

        completion = await provider.complete(a_request())

        assert completion.model == "vendor/fallback-model"


class TestCompletions:
    @pytest.mark.asyncio
    async def test_text_is_returned(self) -> None:
        completion = await make_provider(responds(completion_body("Hi Asha."))).complete(
            a_request()
        )
        assert completion.text == "Hi Asha."

    @pytest.mark.asyncio
    async def test_usage_is_reported(self) -> None:
        """§21: token spend per run is what makes cost visible."""
        completion = await make_provider(responds(completion_body())).complete(a_request())

        assert completion.usage.prompt_tokens == 12
        assert completion.usage.completion_tokens == 7
        assert completion.usage.total_tokens == 19

    @pytest.mark.asyncio
    async def test_a_missing_usage_block_is_tolerated(self) -> None:
        body = completion_body()
        del body["usage"]

        completion = await make_provider(responds(body)).complete(a_request())
        assert completion.usage.total_tokens == 0

    @pytest.mark.asyncio
    async def test_a_null_content_becomes_empty_text(self) -> None:
        """Models return null content when they only call a tool."""
        body = completion_body()
        body["choices"][0]["message"]["content"] = None

        completion = await make_provider(responds(body)).complete(a_request())
        assert completion.text == ""

    @pytest.mark.asyncio
    async def test_no_choices_is_a_permanent_failure(self) -> None:
        with pytest.raises(NonRetryableProviderError, match="no choices"):
            await make_provider(responds({"choices": []})).complete(a_request())

    @pytest.mark.asyncio
    async def test_an_unreadable_success_body_is_permanent(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"not json")

        with pytest.raises(NonRetryableProviderError, match="unreadable"):
            await make_provider(handler).complete(a_request())

    @pytest.mark.asyncio
    async def test_the_request_body_is_compact_but_identical(self) -> None:
        """~5% air on an 18k-char prompt, for free: no whitespace outside strings,
        UTF-8 raw. A parser cannot tell the difference — this test parses it back
        and compares against the pretty form."""
        import json

        seen: dict[str, bytes] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = bytes(request.content)
            return httpx.Response(200, json=completion_body())

        await make_provider(handler).complete(
            CompletionRequest(
                messages=(Message(role=Role.USER, content="Namasté — tomorrow at 10?"),),
            )
        )

        raw = seen["body"]
        assert b" : " not in raw and b", " not in raw
        assert "Namasté".encode() in raw  # raw UTF-8, not \uXXXX
        assert b"\\u00e9" not in raw
        assert json.loads(raw)["messages"][0]["content"] == "Namasté — tomorrow at 10?"


class TestToolCalls:
    @pytest.mark.asyncio
    async def test_tools_are_offered_when_supplied(self) -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            seen.update(json.loads(request.content))
            return httpx.Response(200, json=completion_body())

        spec = ToolSpec(
            name=AuthorizedTool.GET_LEAD,
            description="Get the lead",
            parameters={"type": "object", "properties": {}},
        )
        await make_provider(handler).complete(
            CompletionRequest(messages=(Message(role=Role.USER, content="hi"),), tools=(spec,))
        )

        assert seen["tools"][0]["function"]["name"] == "get_lead"
        # "auto", not forced: most turns are a plain reply, and forcing a tool would
        # make the model invent one.
        assert seen["tool_choice"] == "auto"

    @pytest.mark.asyncio
    async def test_no_tools_key_when_none_are_offered(self) -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            seen.update(json.loads(request.content))
            return httpx.Response(200, json=completion_body())

        await make_provider(handler).complete(a_request())
        assert "tools" not in seen

    @pytest.mark.asyncio
    async def test_a_tool_call_is_parsed(self) -> None:
        body = completion_body(
            "",
            tool_calls=[
                {
                    "id": "call_1",
                    "function": {
                        "name": "book_demo",
                        "arguments": '{"start_time": "2026-09-02T10:00:00+05:30"}',
                    },
                }
            ],
            finish_reason="tool_calls",
        )

        completion = await make_provider(responds(body)).complete(a_request())

        assert completion.tool_calls[0].name == "book_demo"
        assert completion.tool_calls[0].arguments["start_time"] == "2026-09-02T10:00:00+05:30"

    @pytest.mark.asyncio
    async def test_malformed_arguments_do_not_lose_the_turn(self) -> None:
        """Models emit broken JSON. The caller validates arguments anyway, so an empty
        mapping refuses one tool call rather than failing the whole reply."""
        body = completion_body(
            "",
            tool_calls=[{"id": "c1", "function": {"name": "get_lead", "arguments": "{oops"}}],
        )

        completion = await make_provider(responds(body)).complete(a_request())

        assert completion.tool_calls[0].name == "get_lead"
        assert completion.tool_calls[0].arguments == {}

    @pytest.mark.asyncio
    async def test_a_nameless_tool_call_is_dropped(self) -> None:
        body = completion_body("", tool_calls=[{"id": "c1", "function": {}}])

        completion = await make_provider(responds(body)).complete(a_request())
        assert completion.tool_calls == ()


class TestFailureClassification:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 422])
    async def test_client_errors_are_permanent(self, status: int) -> None:
        """Retrying a rejected key, an unknown model or an exhausted balance spends
        the budget and fixes nothing."""
        with pytest.raises(NonRetryableProviderError):
            await make_provider(responds({"error": {"message": "no"}}, status)).complete(
                a_request()
            )

    @pytest.mark.asyncio
    async def test_rate_limiting_is_retryable(self) -> None:
        """[D27]: respected, then retried. The request was never processed, so there
        is nothing to duplicate."""
        with pytest.raises(RetryableProviderError, match="Rate limited"):
            await make_provider(responds({"error": {"message": "slow down"}}, 429)).complete(
                a_request()
            )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    async def test_server_errors_are_retryable(self, status: int) -> None:
        with pytest.raises(RetryableProviderError):
            await make_provider(responds({"error": {"message": "upstream"}}, status)).complete(
                a_request()
            )

    @pytest.mark.asyncio
    async def test_a_timeout_is_retryable(self) -> None:
        """A timeout says nothing about whether the request was processed. The
        caller's idempotency key is what stops the customer getting two replies."""

        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("too slow")

        with pytest.raises(RetryableProviderError, match="timed out"):
            await make_provider(handler).complete(a_request())

    @pytest.mark.asyncio
    async def test_a_network_failure_is_retryable(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route")

        with pytest.raises(RetryableProviderError, match="Network"):
            await make_provider(handler).complete(a_request())

    @pytest.mark.asyncio
    async def test_an_error_inside_a_200_is_caught(self) -> None:
        """OpenRouter reports some upstream failures in the body rather than the
        status, and a caller reading only the status would treat it as a reply."""
        body = {"error": {"code": 502, "message": "upstream vendor failed"}}

        with pytest.raises(RetryableProviderError, match="upstream vendor"):
            await make_provider(responds(body)).complete(a_request())

    @pytest.mark.asyncio
    async def test_a_permanent_error_inside_a_200_is_permanent(self) -> None:
        body = {"error": {"code": 400, "message": "context length exceeded"}}

        with pytest.raises(NonRetryableProviderError, match="context length"):
            await make_provider(responds(body)).complete(a_request())

    @pytest.mark.asyncio
    async def test_an_unknown_status_is_treated_as_transient(self) -> None:
        """The retry budget is finite and bounded; giving up on a recoverable failure
        loses the customer's reply entirely."""
        with pytest.raises(RetryableProviderError):
            await make_provider(responds({"error": {"message": "?"}}, 418)).complete(a_request())


class TestSecurity:
    @pytest.mark.asyncio
    async def test_the_key_is_sent_as_a_bearer_token(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("authorization", "")
            return httpx.Response(200, json=completion_body())

        await make_provider(handler).complete(a_request())
        assert seen["auth"] == "Bearer secret-key"

    @pytest.mark.asyncio
    async def test_the_key_never_appears_in_an_error(self) -> None:
        """§19: an error message ends up in logs, and a leaked key there is a leaked
        key."""
        with pytest.raises(NonRetryableProviderError) as caught:
            await make_provider(responds({"error": {"message": "denied"}}, 401)).complete(
                a_request()
            )

        assert "secret-key" not in str(caught.value)
