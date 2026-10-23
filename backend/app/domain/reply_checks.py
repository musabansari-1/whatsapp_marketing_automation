"""Reply quality checks: a fixed set of customer messages and what a good reply must do.

Run before every prompt or model change (`python -m app.cli eval-replies`), so a change
that breaks a signup link, invents a phone number or answers Tamil in garbled script is
caught before a customer sees it. The cases live in `evals/*.json`; this module holds
only the checks, so they are unit-tested without a model.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.ai.reply_guard import clean_customer_reply

# A run of 10+ digits, optionally spaced or dashed: a phone number the model made up.
_PHONE = re.compile(r"(?:\+?\d[\s-]?){10,}")

_SCRIPT_RANGES = {
    "latin": ((0x0041, 0x024F),),
    "devanagari": ((0x0900, 0x097F),),
    "tamil": ((0x0B80, 0x0BFF),),
    "arabic": ((0x0600, 0x06FF),),
}


@dataclass(frozen=True, slots=True)
class ReplyCase:
    id: str
    message: str
    segment: str | None = None
    history: tuple[tuple[str, str], ...] = ()
    must_contain_any: tuple[str, ...] = ()
    must_not_contain: tuple[str, ...] = ()
    must_not_start_with: tuple[str, ...] = ()
    script: str | None = None
    allow_phone_numbers: bool = False
    max_chars: int = 1200
    notes: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ReplyCase:
        return cls(
            id=str(raw["id"]),
            message=str(raw["message"]),
            segment=raw.get("segment"),
            history=tuple((str(role), str(text)) for role, text in raw.get("history", [])),
            must_contain_any=tuple(raw.get("must_contain_any", [])),
            must_not_contain=tuple(raw.get("must_not_contain", [])),
            must_not_start_with=tuple(raw.get("must_not_start_with", [])),
            script=raw.get("script"),
            allow_phone_numbers=bool(raw.get("allow_phone_numbers", False)),
            max_chars=int(raw.get("max_chars", 1200)),
            notes=str(raw.get("notes", "")),
        )


@dataclass(slots=True)
class CaseResult:
    case: ReplyCase
    reply: str
    failures: list[str] = field(default_factory=list)
    latency_ms: int = 0
    rounds: int = 1

    @property
    def passed(self) -> bool:
        return not self.failures


def load_cases(path: Path) -> list[ReplyCase]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    cases = [ReplyCase.from_dict(item) for item in raw["cases"]]
    ids = [case.id for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError(f"{path}: case ids must be unique")
    return cases


def check_reply(case: ReplyCase, reply: str) -> list[str]:
    """Every way `reply` fails `case`; empty when it passes."""
    failures: list[str] = []
    text = reply.strip()

    if not text:
        return ["empty reply"]
    if clean_customer_reply(text).strip() != text:
        failures.append("the reply guard would change or blank this reply")
    if len(text) > case.max_chars:
        failures.append(f"too long: {len(text)} > {case.max_chars} characters")

    lowered = text.casefold()
    if case.must_contain_any and not any(
        item.casefold() in lowered for item in case.must_contain_any
    ):
        failures.append(f"contains none of {list(case.must_contain_any)}")
    for item in case.must_not_contain:
        if item.casefold() in lowered:
            failures.append(f"contains {item!r}")
    for item in case.must_not_start_with:
        if lowered.lstrip("*_ ").startswith(item.casefold()):
            failures.append(f"starts with {item!r}")
    if not case.allow_phone_numbers and _PHONE.search(text):
        failures.append("contains a phone number")
    if case.script and not _has_script(text, case.script):
        failures.append(f"not written in {case.script} script")

    return failures


def _has_script(text: str, script: str) -> bool:
    ranges = _SCRIPT_RANGES.get(script.casefold())
    if ranges is None:
        raise ValueError(f"unknown script {script!r}")
    return any(low <= ord(char) <= high for char in text for low, high in ranges)


__all__ = ["CaseResult", "ReplyCase", "check_reply", "load_cases"]
