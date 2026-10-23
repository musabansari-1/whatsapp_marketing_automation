"""Hot-lead bell, team handover, media replies, Hindi opt-out and reply speed.

Pure-function tests: the decisions each feature makes, without a database or network.
The handlers that act on them are covered by the integration suite.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.ai.agent import _reply_written_with_facts
from app.ai.tools import ToolResult, _attention_reason
from app.core.config import Settings
from app.domain.whatsapp_webhook import REPLYABLE_MEDIA_TYPES, matches_opt_out, parse_webhook
from app.integrations.llm import (
    AuthorizedTool,
    CompletionRequest,
    LLMCompletion,
    Message,
    Role,
    ToolCall,
)
from app.integrations.llm.openrouter import OpenRouterLLMProvider
from app.worker.handlers.conversation import is_ai_paused

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)


class TestAttentionReason:
    def test_new_registration_intent_rings(self) -> None:
        assert _attention_reason({}, {"registration_intent": True}) == "wants to register"

    def test_a_request_for_a_person_rings_and_wins(self) -> None:
        reason = _attention_reason({}, {"registration_intent": True, "human_requested": "yes"})
        assert reason == "asked to speak to a person"

    def test_a_fact_already_recorded_does_not_ring_again(self) -> None:
        """Facts are re-recorded on many turns; a bell per turn teaches the team to
        ignore it."""
        assert (
            _attention_reason({"registration_intent": True}, {"registration_intent": True}) is None
        )

    def test_interest_alone_does_not_ring(self) -> None:
        assert _attention_reason({}, {"interested": True}) is None


def _completion(text: str, *names: str) -> LLMCompletion:
    calls = tuple(ToolCall(id=f"c{i}", name=name, arguments={}) for i, name in enumerate(names))
    return LLMCompletion(text=text, model="m", tool_calls=calls, finish_reason="tool_calls")


def _results(completion: LLMCompletion, result: ToolResult) -> list[tuple[ToolCall, ToolResult]]:
    return [(call, result) for call in completion.tool_calls]


class TestReplyWrittenWithFacts:
    """The latency shortcut: skip the second model round only when it adds nothing."""

    OK = ToolResult(ok=True, data={"recorded": True, "still_needed": []})

    def test_a_reply_written_alongside_update_lead_is_final(self) -> None:
        completion = _completion("Here is your doctor signup link.", AuthorizedTool.UPDATE_LEAD)
        assert _reply_written_with_facts(completion, _results(completion, self.OK))

    def test_no_text_means_another_round(self) -> None:
        completion = _completion("", AuthorizedTool.UPDATE_LEAD)
        assert not _reply_written_with_facts(completion, _results(completion, self.OK))

    def test_tool_plumbing_is_not_a_reply(self) -> None:
        completion = _completion('{"name": "update_lead"}', AuthorizedTool.UPDATE_LEAD)
        assert not _reply_written_with_facts(completion, _results(completion, self.OK))

    def test_an_informational_tool_needs_its_result_first(self) -> None:
        completion = _completion("Let me check.", AuthorizedTool.GET_AVAILABLE_SLOTS)
        assert not _reply_written_with_facts(completion, _results(completion, self.OK))

    def test_fields_still_needed_must_be_asked_for(self) -> None:
        completion = _completion("Thanks!", AuthorizedTool.UPDATE_LEAD)
        needing = ToolResult(ok=True, data={"still_needed": ["team_size"]})
        assert not _reply_written_with_facts(completion, _results(completion, needing))

    def test_a_failed_tool_gets_another_round(self) -> None:
        completion = _completion("Thanks!", AuthorizedTool.UPDATE_LEAD)
        failed = ToolResult(ok=False, error="boom")
        assert not _reply_written_with_facts(completion, _results(completion, failed))


class TestOpenRouterSpeedOptions:
    async def _sent_payload(self, **options: Any) -> dict[str, Any]:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(json.loads(request.content))
            return httpx.Response(
                200, json={"choices": [{"message": {"content": "hi"}}], "model": "m"}
            )

        provider = OpenRouterLLMProvider(
            api_key="k",
            model="vendor/model",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            **options,
        )
        await provider.complete(
            CompletionRequest(messages=(Message(role=Role.USER, content="hello"),))
        )
        return seen

    async def test_latency_routing_and_reasoning_effort_are_sent(self) -> None:
        payload = await self._sent_payload(provider_sort="latency", reasoning_effort="low")
        assert payload["provider"] == {"sort": "latency"}
        assert payload["reasoning"] == {"effort": "low"}

    async def test_nothing_is_sent_when_unset(self) -> None:
        payload = await self._sent_payload()
        assert "provider" not in payload
        assert "reasoning" not in payload

    def test_latency_routing_is_the_default(self) -> None:
        assert Settings().openrouter_provider_sort == "latency"


class TestHindiOptOut:
    KEYWORDS = Settings().opt_out_keywords

    @pytest.mark.parametrize("text", ["बंद करो", "band karo", "Band", "MAT BHEJO", "मत भेजो।"])
    def test_hindi_and_hinglish_opt_out(self, text: str) -> None:
        assert matches_opt_out(text.rstrip("।"), self.KEYWORDS)

    @pytest.mark.parametrize("text", ["band mat karo", "बंद नहीं करना", "doctor signup"])
    def test_incidental_hindi_does_not_opt_out(self, text: str) -> None:
        assert not matches_opt_out(text, self.KEYWORDS)


def _media_payload(message_type: str, caption: str | None = None) -> dict[str, Any]:
    media: dict[str, Any] = {"id": "media-1"}
    if caption is not None:
        media["caption"] = caption
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": "1"},
                            "messages": [
                                {
                                    "id": "wamid.m1",
                                    "from": "919315759252",
                                    "timestamp": "1761200000",
                                    "type": message_type,
                                    message_type: media,
                                }
                            ],
                        }
                    }
                ]
            }
        ]
    }


class TestMedia:
    def test_a_photo_caption_is_read_as_text(self) -> None:
        parsed = parse_webhook(_media_payload("image", "Is this medicine available?")).inbound[0]
        assert parsed.text == "Is this medicine available?"

    def test_a_voice_note_has_no_text_but_is_answerable(self) -> None:
        parsed = parse_webhook(_media_payload("audio")).inbound[0]
        assert parsed.text is None
        assert parsed.message_type in REPLYABLE_MEDIA_TYPES

    def test_a_sticker_is_not_answered(self) -> None:
        assert "sticker" not in REPLYABLE_MEDIA_TYPES

    def test_a_blank_caption_is_no_text(self) -> None:
        assert parse_webhook(_media_payload("image", "  ")).inbound[0].text is None


class TestAssistantPause:
    def test_paused_until_a_future_time(self) -> None:
        conversation = SimpleNamespace(ai_paused_until=NOW + timedelta(hours=1))
        assert is_ai_paused(conversation, now=NOW)  # type: ignore[arg-type]

    @pytest.mark.parametrize("until", [None, NOW, NOW - timedelta(minutes=1)])
    def test_not_paused_when_unset_or_past(self, until: datetime | None) -> None:
        conversation = SimpleNamespace(ai_paused_until=until)
        assert not is_ai_paused(conversation, now=NOW)  # type: ignore[arg-type]
