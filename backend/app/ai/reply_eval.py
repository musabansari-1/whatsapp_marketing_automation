"""Run the reply quality cases against the real model, from the catalog file.

Reads the prompt, links and CTAs from `config/products.json` rather than the database,
so a change is checked *before* `configure-products` makes it live. No knowledge base
is attached (the agent is told it has none), so the cases test behaviour the prompt
owns: links, hand-offs, language, safety and invention, not product facts.

Tools are offered exactly as in a live turn but answered with stand-ins: `update_lead`
always records, everything else is unavailable. Nothing is written and no WhatsApp
message is sent; the only network call is to the model.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO
from zoneinfo import ZoneInfo

from app.ai import tools as ai_tools
from app.ai.agent import MAX_TOOL_ROUNDS, _build_messages, _reply_written_with_facts
from app.ai.reply_guard import clean_customer_reply
from app.configuration.catalog import ProductEntry, load_catalog
from app.domain.ai_config import with_audience_segment_context
from app.domain.conversion import (
    uses_calendar_booking,
    with_conversion_guidance,
    with_today_context,
)
from app.domain.reply_checks import CaseResult, ReplyCase, check_reply, load_cases
from app.integrations.llm import (
    AuthorizedTool,
    CompletionRequest,
    LLMProvider,
    Message,
    Role,
)

_RECORDED = ai_tools.ToolResult(ok=True, data={"recorded": True, "still_needed": []})
_UNAVAILABLE = ai_tools.ToolResult(ok=False, error="not available in this check")


def system_prompt_for(product: ProductEntry, segment_slug: str | None) -> tuple[str, str]:
    """The system prompt and fallback a live turn in this segment would use."""
    base = product.ai_configuration
    override = product.segment_ai_overrides.get(segment_slug) if segment_slug else None
    prompt = (override and override.system_prompt) or (base and base.system_prompt) or ""
    fallback = (override and override.fallback_message) or (base and base.fallback_message) or ""
    if not prompt:
        raise ValueError(f"{product.slug} has no system prompt for {segment_slug or 'the product'}")

    segment = next((s for s in product.segments if s.slug == segment_slug), None)
    prompt = with_conversion_guidance(
        prompt,
        mode=product.conversion_mode,
        registration_url=(segment and segment.registration_url) or product.registration_url,
        cta_label=segment.cta_label if segment else None,
        cta_instruction=segment.cta_instruction if segment else None,
    )
    prompt = with_audience_segment_context(
        prompt, segment_display_name=segment.display_name if segment else None
    )
    zone = ZoneInfo(product.demo_timezone or "Asia/Kolkata")
    return with_today_context(prompt, now=datetime.now(UTC), zone=zone), fallback


async def run_case(provider: LLMProvider, product: ProductEntry, case: ReplyCase) -> CaseResult:
    system_prompt, fallback = system_prompt_for(product, case.segment)
    history = [Message(role=Role(role), content=text) for role, text in case.history]
    history.append(Message(role=Role.USER, content=case.message))
    messages = _build_messages(
        system_prompt=system_prompt, history=history, retrieval=None, fallback_message=fallback
    )
    specs = ai_tools.tool_specs(include_booking=uses_calendar_booking(product.conversion_mode))

    started = time.monotonic()
    text = ""
    rounds = 0
    for rounds in range(1, MAX_TOOL_ROUNDS + 1):
        completion = await provider.complete(
            CompletionRequest(
                messages=tuple(messages), tools=specs if rounds < MAX_TOOL_ROUNDS else ()
            )
        )
        text = completion.text or ""
        if not completion.tool_calls:
            break
        results = [
            (call, _RECORDED if call.name == AuthorizedTool.UPDATE_LEAD else _UNAVAILABLE)
            for call in completion.tool_calls
        ]
        if _reply_written_with_facts(completion, results):
            break
        messages.append(Message(role=Role.ASSISTANT, content=text or "(requesting tools)"))
        for call, result in results:
            messages.append(
                Message(
                    role=Role.TOOL,
                    content=json.dumps(result.as_payload()),
                    tool_call_id=call.id,
                )
            )

    reply = clean_customer_reply(text.strip()) or text.strip()
    latency_ms = int((time.monotonic() - started) * 1000)
    return CaseResult(
        case=case,
        reply=reply,
        failures=check_reply(case, text),
        latency_ms=latency_ms,
        rounds=rounds,
    )


async def run_reply_eval(
    provider: LLMProvider,
    *,
    catalog_path: Path,
    product_slug: str,
    cases_path: Path,
    out: TextIO,
    only: str | None = None,
) -> list[CaseResult]:
    products = {entry.slug: entry for entry in load_catalog(catalog_path).products}
    if product_slug not in products:
        raise ValueError(f"{product_slug!r} is not in {catalog_path}")
    product = products[product_slug]

    results: list[CaseResult] = []
    for case in load_cases(cases_path):
        if only and case.id != only:
            continue
        result = await run_case(provider, product, case)
        results.append(result)
        verdict = "PASS" if result.passed else "FAIL"
        out.write(f"{verdict}  {case.id}  ({result.latency_ms} ms, {result.rounds} round(s))\n")
        for failure in result.failures:
            out.write(f"      - {failure}\n")
        if not result.passed:
            out.write(f"      reply: {result.reply[:400]!r}\n")

    passed = sum(result.passed for result in results)
    if results:
        slowest = max(results, key=lambda r: r.latency_ms)
        average = sum(r.latency_ms for r in results) // len(results)
        out.write(
            f"\n{passed}/{len(results)} passed. Model time: average {average} ms, "
            f"slowest {slowest.latency_ms} ms ({slowest.case.id}).\n"
        )
    return results


__all__ = ["run_case", "run_reply_eval", "system_prompt_for"]
