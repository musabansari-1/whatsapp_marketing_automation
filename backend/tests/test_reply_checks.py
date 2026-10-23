"""Reply quality checks and the shipped Life Herba case set."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from app.ai.reply_eval import run_case, run_reply_eval, system_prompt_for
from app.configuration.catalog import ProductEntry, load_catalog
from app.domain.reply_checks import ReplyCase, check_reply, load_cases
from app.integrations.llm import FakeLLMProvider, LLMCompletion, ToolCall

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "config" / "products.json"
CASES = ROOT / "evals" / "life-herba.json"


def _life_herba() -> ProductEntry:
    return {p.slug: p for p in load_catalog(CATALOG).products}["life-herba"]


class TestChecks:
    CASE = ReplyCase(
        id="doctor",
        message="How do I join?",
        must_contain_any=("lifeherbagroup.com/join/doctor",),
        must_not_start_with=("No",),
    )

    def test_a_good_reply_passes(self) -> None:
        reply = "Welcome! Sign up here: https://lifeherbagroup.com/join/doctor?utm_source=whatsapp"
        assert check_reply(self.CASE, reply) == []

    def test_a_missing_link_fails(self) -> None:
        assert check_reply(self.CASE, "Please visit our website.") == [
            "contains none of ['lifeherbagroup.com/join/doctor']"
        ]

    def test_an_invented_phone_number_fails(self) -> None:
        reply = "Call us on +91 98765 43210 or lifeherbagroup.com/join/doctor"
        assert "contains a phone number" in check_reply(self.CASE, reply)

    def test_a_refusal_opening_fails(self) -> None:
        assert any(
            "starts with" in f
            for f in check_reply(self.CASE, "No, but lifeherbagroup.com/join/doctor")
        )

    def test_mixed_scripts_fail_through_the_reply_guard(self) -> None:
        case = ReplyCase(id="tamil", message="வணக்கம்")
        failures = check_reply(case, "வணக்கம்! ஒரு காதல் உதவியை தேவை? नमस्ते")
        assert "the reply guard would change or blank this reply" in failures

    def test_the_wrong_script_fails(self) -> None:
        case = ReplyCase(id="hindi", message="Hindi mein", script="devanagari")
        assert check_reply(case, "LifeHerba is an AYUSH app.") == [
            "not written in devanagari script"
        ]
        assert check_reply(case, "LifeHerba एक आयुष ऐप है।") == []


class TestShippedCases:
    def test_the_case_file_loads_and_every_segment_exists(self) -> None:
        segments = {segment.slug for segment in _life_herba().segments}
        cases = load_cases(CASES)
        assert len(cases) >= 10
        for case in cases:
            assert case.segment is None or case.segment in segments, case.id

    def test_every_segment_prompt_resolves_with_its_own_link(self) -> None:
        product = _life_herba()
        for segment in product.segments:
            prompt, fallback = system_prompt_for(product, segment.slug)
            assert segment.registration_url and segment.registration_url in prompt
            assert fallback


class TestRunner:
    CASE = ReplyCase(
        id="doctor",
        segment="healthcare-practitioners",
        message="How do I join?",
        must_contain_any=("lifeherbagroup.com/join/doctor",),
    )

    async def test_a_reply_written_with_the_facts_takes_one_round(self) -> None:
        llm = FakeLLMProvider()
        llm.queue_completion(
            LLMCompletion(
                text="Sign up here: https://lifeherbagroup.com/join/doctor",
                model=llm.model,
                tool_calls=(
                    ToolCall(
                        id="c1",
                        name="update_lead",
                        arguments={"facts": {"registration_intent": True}},
                    ),
                ),
                finish_reason="tool_calls",
            )
        )

        result = await run_case(llm, _life_herba(), self.CASE)

        assert result.passed, result.failures
        assert result.rounds == 1
        assert llm.call_count == 1

    async def test_a_tool_only_round_is_followed_by_the_reply(self) -> None:
        llm = FakeLLMProvider()
        llm.queue_tool_call("update_lead", {"facts": {"registration_intent": True}})
        llm.queue_text("Here you go: https://lifeherbagroup.com/join/doctor")

        result = await run_case(llm, _life_herba(), self.CASE)

        assert result.passed
        assert result.rounds == 2

    async def test_the_report_names_failures(self) -> None:
        out = io.StringIO()
        llm = FakeLLMProvider(default_text="Please visit our website.")

        results = await run_reply_eval(
            llm,
            catalog_path=CATALOG,
            product_slug="life-herba",
            cases_path=CASES,
            out=out,
            only="doctor-wants-to-join",
        )

        assert len(results) == 1 and not results[0].passed
        assert "FAIL  doctor-wants-to-join" in out.getvalue()

    async def test_an_unknown_product_is_refused(self) -> None:
        with pytest.raises(ValueError, match="not in"):
            await run_reply_eval(
                FakeLLMProvider(),
                catalog_path=CATALOG,
                product_slug="nope",
                cases_path=CASES,
                out=io.StringIO(),
            )
