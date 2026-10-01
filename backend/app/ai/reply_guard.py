"""Customer-facing reply hygiene: prose out, tool plumbing never (§9, §11, §21).

A model occasionally narrates the call it is making instead of — or next to — the sentence
it means the customer to read::

    Got it, I'll note that down.
    {"name": "update_lead", "arguments": {"facts": {"interested": true}}}

On a WhatsApp thread that is a defect, not a style choice. It puts internal qualification
field names in front of a customer — including the labels the prompts explicitly forbid
showing ("Do not expose these labels to the user") — and it reads as a broken product.

A second defect rides along once a booking exists: the model tells the customer a
calendar invite was emailed to them (staging 2026-09-28, after the prompts already
forbade it). The platform has no email path at all — no attendee is ever added and
`sendUpdates` stays `none` — so that claim is unconditionally false, and removing it
can never delete a true statement.

This module is the last gate before a turn is sent. It is pure and model-agnostic, and
deliberately narrow in what it *keeps*: prose the model composed is never rewritten. Four
rules decide what goes:

- anything that reads as tool plumbing — a tool name, or a key like `arguments`, `facts`,
  `lead` or `qualification_judgement` — is removed wherever it appears;
- a JSON object or array sitting alone on its own line is removed even when its keys are
  unrecognised, because a serialised blob is not a reply whatever it contains;
- a sentence claiming an emailed calendar invite is removed, as is a trailing
  "and …" clause making that claim — while the true half of a mixed sentence (the
  time, the Meet link) survives;
- a sentence claiming phone/device capabilities the platform does not have is
  removed, with battery telemetry going only when such a claim was found in the
  same reply (energy talk about battery storage must survive on its own).

Unparseable text and JSON embedded in the middle of a sentence are left alone: half-
deleting something is worse than letting a human see it. The same caution applies to
the invite and device rules: a sentence carrying a negation ("no invite will be
emailed", "I can't record your screen") is left alone, and novel phrasings the
patterns do not recognise still pass — the prompts and the tool result remain the
first layers.

Delivery is not re-tried here. When nothing survives the strip the caller falls back to
the approved wording ([D21]) exactly as it does for an empty completion.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Iterator
from typing import Any

from app.integrations.llm import AuthorizedTool

#: The tool names as a model spells them when it echoes a call it made.
_TOOL_NAMES = frozenset(tool.value for tool in AuthorizedTool)

#: Keys that appear only in tool arguments or in the lead record a tool returned. A JSON
#: object carrying one of these is plumbing, whatever the model meant by printing it.
#: The qualification labels are here because the prompts forbid showing them.
_PAYLOAD_KEYS = frozenset(
    {
        "arguments",
        "function",
        "functions",
        "tool_call",
        "tool_calls",
        "tool_call_id",
        "facts",
        "known_facts",
        "qualification_data",
        "qualification_judgement",
        "interested",
        "not_interested",
        "demo_intent",
        "registration_intent",
        "lead",
        "lead_id",
        "still_needed",
        "recorded",
        "ok",
    }
)

# Chat templates and models wrap the same JSON in markup; the payload inside is caught by
# the span scan, so the tags themselves are simply dropped.
_TOOL_CALL_MARKUP = re.compile(
    r"<\s*/?\s*(?:tool_call|tool_calls|function_call|function_calls)\b[^>]*>",
    re.IGNORECASE,
)

# ```…``` blocks. The body is kept when it is prose (the fence markers are presentation)
# and dropped whole when it is a payload.
_FENCED_BLOCK = re.compile(
    r"```+[ \t]*[A-Za-z0-9_+-]*[ \t]*\r?\n?(?P<body>.*?)(?:```+|\Z)",
    re.DOTALL,
)

_LEFTOVER_FENCE = re.compile(r"```+")
#: Brackets are in the class because a model very often labels the call it is printing
#: rather than emitting the bare object — "(book_demo)" over a line of arguments. After the
#: tool name is removed, "()" is all that is left, and it must read as residue, not prose.
_PUNCTUATION_ONLY = re.compile(r"^[\s`*_~#>•·|,;:\-–—()\[\]{}]+$")
_SPACE_BEFORE_PUNCTUATION = re.compile(r"[ \t]+([,.;:!?])")
_SPACE_RUN = re.compile(r"(?<=\S)[ \t]{2,}")
_BLANK_RUN = re.compile(r"\n{3,}")
#: Matches a bare tool name left behind where its payload used to be ("update_lead:").
_TOOL_NAMES_PATTERN = re.compile("|".join(sorted(_TOOL_NAMES)), re.IGNORECASE)

#: A whole line that is nothing but a tool name, optionally wrapped in brackets or quotes and
#: optionally followed by a colon — "(book_demo)", "[get_lead]:". This is a label the model
#: printed above its arguments, and on its own it says nothing to a customer. Anchored to the
#: whole line so a tool name inside real prose ("I could not use update_lead for this") is
#: never touched.
_TOOL_LABEL_LINE = re.compile(
    rf"^[\s\[\(\{{\"'`]*(?:{_TOOL_NAMES_PATTERN.pattern})[\s\]\)\}}\"'`]*[:;,.]?\s*$",
    re.IGNORECASE,
)

#: Phone/device actions claimed as the model's own. The platform is a text-only
#: channel with no device APIs, so such claims are unconditionally false — but only
#: the claiming shapes are matched. "Check your phone for my message" is ordinary
#: advice (check + phone deliberately excluded), "share your screen on the call"
#: uses no claiming verb, and metaphors ("I see you chose noon") name no device.
#: Like the invite rule, anything carrying a negation is left alone.
_DEVICE_CLAIM = re.compile(
    r"\b(record|access)\b.{0,50}\b(screen|status bar|battery|camera|mic|microphone|device|phone)\b"
    r"|\bI(?:'ll| will| can)\b.{0,60}\b(check|see|watch|monitor)\b.{0,60}"
    r"\b(screen|status bar|battery|camera|mic|microphone|device)\b",
    re.IGNORECASE | re.DOTALL,
)

#: Telemetry readings ("the battery level is at 70%"). Indistinguishable from
#: legitimate energy talk on their own — SustainX sells battery storage — so these
#: only go when a device claim was already found in the same reply. A contaminated
#: reply's telemetry is part of the fabrication; standalone energy prose is kept.
_BATTERY_TELEMETRY = re.compile(
    r"\bbattery\b.{0,40}\d+\s*(%|percent\b)",
    re.IGNORECASE | re.DOTALL,
)

#: An invite claimed sent or delivered through email. The platform has no email path,
#: so any such claim is false — but only the emailed variants are matched. A bare
#: "send you the invite" with no channel may mean WhatsApp, which is true, and must
#: survive; likewise anything carrying a negation ("no invite will be emailed").
_INVITE_EMAILED = re.compile(
    r"invit\w*.{0,50}\b(e-?mailed|sent|received)\b"
    r"|\b(e-?mailed|sent|received).{0,50}invit\w*",
    re.IGNORECASE | re.DOTALL,
)
_INVITE_BY_EMAIL = re.compile(
    r"invit\w*.{0,50}\b(inbox|by e-?mail|via e-?mail|to your e-?mail|in your e-?mail)\b"
    r"|\binbox\b.{0,50}invit\w*",
    re.IGNORECASE | re.DOTALL,
)
_NEGATED = re.compile(
    r"\b(no|not|never|n't|without|don't|doesn't|didn't|won't|can't|cannot|isn't|aren't)\b",
    re.IGNORECASE,
)

#: A trailing "and …" clause making the claim, so the true half of a mixed sentence
#: survives: "The Meet link is … and the invite was emailed" keeps the link. Anchored
#: to the clause carrying the emailed/sent verb, so a legit "and the invite link is
#: below" is never touched.
_TRAILING_INVITE_CLAUSE = re.compile(
    r",?\s+and\s+(?:the\s+|a\s+)?(?:calendar\s+)?invit\w*"
    r"(?=[\s\S]{0,80}\b(e-?mailed|sent|received)\b)[\s\S]*?(?=[.!?]$)",
    re.IGNORECASE,
)

#: Sentence boundaries for the claim scan. URLs carry periods without trailing space,
#: so only punctuation followed by whitespace (or the end) splits.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")

_STRONG_PLUMBING = _TOOL_NAMES | frozenset(
    {
        "arguments",
        "facts",
        "known_facts",
        "qualification_data",
        "qualification_judgement",
        "interested",
        "not_interested",
        "demo_intent",
        "registration_intent",
        "lead_id",
        "still_needed",
        "recorded",
        "tool_call",
        "tool_calls",
        "tool_call_id",
        "function",
        "functions",
    }
)

_STRONG_PLUMBING_PATTERN = "|".join(
    re.escape(token) for token in sorted(_STRONG_PLUMBING, key=len, reverse=True)
)

_QUOTED_STRONG_TOKEN = re.compile(
    rf"""["'](?:{_STRONG_PLUMBING_PATTERN})["']""",
    re.IGNORECASE,
)

#: Where a JSON blob begins: a brace followed by a quote, or a quoted plumbing token. A
#: bare `{` is not enough — "{Name}, thanks for your reply" is prose.
_BLOB_LINE = re.compile(
    rf"""\{{\s*["']|\[\s*[\{{"']|{_QUOTED_STRONG_TOKEN.pattern}""",
    re.IGNORECASE,
)

_OPENERS = {"{": "}", "[": "]"}

_UNPARSEABLE = object()


def clean_customer_reply(text: str) -> str:
    """Return the reply with leaked tool payloads removed.

    Returns an empty string when nothing but plumbing was there, which the caller turns
    into the approved fallback — the same path an empty completion already takes.
    """
    if not text:
        return ""

    cleaned = _TOOL_CALL_MARKUP.sub("", text)
    cleaned = _FENCED_BLOCK.sub(_without_payload_fences, cleaned)
    cleaned = _LEFTOVER_FENCE.sub("", cleaned)
    cleaned = _strip_payload_spans(cleaned)
    cleaned = _cut_blob_lines(cleaned)
    cleaned = _drop_tool_label_lines(cleaned)
    cleaned = _without_emailed_invite_claims(cleaned)
    cleaned = _without_device_claims(cleaned)

    if _has_json_residue(cleaned):
        # What is left is still a payload: a blob that was cut off mid-object, or quoted
        # plumbing the span pass could not parse. Sending it would defeat the point of the
        # guard, so the reply becomes empty and the caller sends the approved fallback.
        # Fail closed: a customer receives either clean prose or the approved wording.
        return ""

    if cleaned == text:
        # Untouched replies are returned as written: a model that wrote nothing wrong must
        # not have its formatting normalised as a side effect of this guard.
        return text.strip()

    return _tidy(cleaned)


# ---------------------------------------------------------------- internals


def _without_payload_fences(match: re.Match[str]) -> str:
    """Drop a fenced block that is a payload; otherwise unwrap it and keep the prose."""
    body = match.group("body")
    if _is_leak(body.strip(), standalone=True):
        return ""
    return body


def _sits_alone(text: str, start: int, end: int) -> bool:
    """Whether a span occupies whole line(s) rather than sitting inside a sentence."""
    before = text[:start]
    after = text[end:]
    starts_a_line = before == "" or before.rstrip(" \t").endswith("\n")
    ends_a_line = after == "" or after.split("\n", 1)[0].strip() == ""
    return starts_a_line and ends_a_line


def _cut_blob_lines(text: str) -> str:
    """Keep the prose on each line, up to the point where a JSON blob starts on it.

    This is what catches a blob the span pass could not parse — truncated output, or a
    payload whose braces never closed — without discarding the sentence in front of it.

    Only a blob that *leads* its line is cut. One sitting mid-sentence is left for
    `_has_json_residue` to judge: cutting it there would turn a model quoting a value
    ("The configuration is {\\"city\\": \\"Pune\\"}.") into a truncated fragment that says
    nothing, which is a worse defect than the one being guarded against.
    """
    kept: list[str] = []
    for line in text.splitlines():
        match = _BLOB_LINE.search(line)
        if match and _PUNCTUATION_ONLY.match(line[: match.start()]):
            kept.append(line[: match.start()])
        else:
            kept.append(line)
    return "\n".join(kept)


def _drop_tool_label_lines(text: str) -> str:
    """Remove whole lines that are only a tool name — "(book_demo)", "[get_lead]:".

    A model that narrates a call often prints the name above the arguments, so removing the
    arguments alone can leave the label sitting in the reply as the customer's whole message.
    The pattern is anchored to the line, so a name mentioned inside a sentence is untouched.
    """
    return "\n".join(line for line in text.splitlines() if not _TOOL_LABEL_LINE.match(line))


def _without_emailed_invite_claims(text: str) -> str:
    """Remove sentences claiming a calendar invite was emailed.

    The claim is unconditionally false — the platform has no email path — so only the
    shape of the match matters, never its content. A trailing "and …" clause making
    the claim is cut while the true half of the sentence survives; a sentence that is
    nothing but the claim goes whole. Anything carrying a negation is left alone, as
    is any phrasing the patterns do not recognise: the prompts and the tool result
    remain the first two layers for those.

    Spacing is preserved exactly when nothing matches, so clean replies are returned
    as written; any removal routes through the shared tidy-up downstream.
    """
    sentences = _SENTENCE_SPLIT.split(text)
    separators = _SENTENCE_SPLIT.findall(text)
    kept: list[str] = []
    changed = False
    for index, sentence in enumerate(sentences):
        prefix = separators[index - 1] if index else ""
        if not sentence.strip():
            kept.append(prefix + sentence)
            continue
        stripped, count = _TRAILING_INVITE_CLAUSE.subn("", sentence)
        if count:
            kept.append(prefix + stripped)
            changed = True
        elif _claims_emailed_invite(sentence):
            changed = True
        else:
            kept.append(prefix + sentence)
    return "".join(kept) if changed else text


def _claims_emailed_invite(sentence: str) -> bool:
    """Whether this sentence states an invite went out by email."""
    if _NEGATED.search(sentence):
        return False
    return bool(_INVITE_EMAILED.search(sentence) or _INVITE_BY_EMAIL.search(sentence))


def _without_device_claims(text: str) -> str:
    """Remove sentences claiming phone/device capabilities or telemetry.

    Same shape as the invite rule, narrower target: first-person or imperative
    claims over device nouns, plus battery telemetry only when a claim was found
    in the same reply (energy talk about battery storage must survive on its
    own). Negations are left alone — a denial ("I can't record your screen") is
    a true statement the customer should read.
    """
    sentences = _SENTENCE_SPLIT.split(text)
    separators = _SENTENCE_SPLIT.findall(text)
    kept: list[str] = []
    changed = False
    for index, sentence in enumerate(sentences):
        prefix = separators[index - 1] if index else ""
        if not sentence.strip():
            kept.append(prefix + sentence)
            continue
        if _claims_device_capability(sentence):
            changed = True
        else:
            kept.append(prefix + sentence)
    if not changed:
        return text
    stripped = "".join(kept)
    # Second pass over what remains: telemetry readings are only suspicious
    # inside a reply already caught fabricating device actions.
    kept = []
    changed = False
    again = _SENTENCE_SPLIT.split(stripped)
    separators = _SENTENCE_SPLIT.findall(stripped)
    for index, sentence in enumerate(again):
        prefix = separators[index - 1] if index else ""
        if sentence.strip() and _is_battery_telemetry(sentence):
            changed = True
            continue
        kept.append(prefix + sentence)
    return "".join(kept) if changed else stripped


def _claims_device_capability(sentence: str) -> bool:
    """Whether this sentence claims a phone/device action as the model's own."""
    if _NEGATED.search(sentence):
        return False
    return bool(_DEVICE_CLAIM.search(sentence))


def _is_battery_telemetry(sentence: str) -> bool:
    """Whether this sentence reports a battery reading."""
    if _NEGATED.search(sentence):
        return False
    return bool(_BATTERY_TELEMETRY.search(sentence))


def _has_json_residue(text: str) -> bool:
    """Whether anything left still reads as a serialised payload.

    Checked after the removals, and deliberately blunt: an unclosed brace or bracket, an
    unclosed double-quoted run, or a quoted plumbing token. Any of those means the reply
    is not clean prose, and the caller's approved fallback is the honest thing to send.
    """
    stack: list[str] = []
    in_string = False
    escaped = False

    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char in _OPENERS:
            stack.append(char)
        elif char in "}]" and (not stack or _OPENERS[stack.pop()] != char):
            return True

    if stack or in_string:
        return True

    return _QUOTED_STRONG_TOKEN.search(text) is not None


def _strip_payload_spans(text: str) -> str:
    """Remove every balanced JSON span that reads as a tool call or a lead record."""
    pieces: list[str] = []
    cursor = 0

    for start, end in _iter_json_spans(text):
        if start < cursor:  # pragma: no cover - spans do not nest
            continue
        span = text[start:end]
        if _is_leak(span, standalone=_sits_alone(text, start, end)):
            pieces.append(text[cursor:start])
            cursor = end

    if cursor == 0:
        return text

    pieces.append(text[cursor:])
    return "".join(pieces)


def _iter_json_spans(text: str) -> Iterator[tuple[int, int]]:
    """Yield ``(start, end)`` for each top-level ``{…}`` / ``[…]`` span in ``text``.

    Braces inside JSON strings are ignored, so a payload containing a quoted ``}`` in an
    error message still ends where it should. Text that never closes is not a span: an
    unterminated blob is left for a human to see rather than half-deleted.
    """
    stack: list[str] = []
    start: int | None = None
    in_string = False
    escaped = False

    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char in "{[":
            if not stack:
                start = index
            stack.append(char)
        elif char in "}]":
            if not stack:
                continue
            opener = stack.pop()
            if (opener, char) not in (("{", "}"), ("[", "]")):
                stack.clear()
                start = None
            elif not stack and start is not None:
                yield start, index + 1
                start = None


def _is_leak(span: str, *, standalone: bool = False) -> bool:
    """Whether this JSON span is payload rather than something a customer should read.

    ``standalone`` is set when the span occupies whole line(s), which is what makes an
    unrecognised object a leak: a serialised blob alone in a reply is never the reply.
    Inline JSON with no plumbing key is left in place, deliberately, so a model quoting
    a value mid-sentence is not silently censored.
    """
    if not span.startswith(("{", "[")):
        return False

    data = _parse(span)
    if data is _UNPARSEABLE:
        return False

    if _carries_plumbing(data):
        return True

    return standalone and isinstance(data, (dict, list)) and bool(data)


def _parse(span: str) -> Any:
    """Read a JSON literal, tolerating the single-quoted variant models also emit."""
    try:
        return json.loads(span)
    except ValueError:
        pass
    try:
        # Literals only — `ast.literal_eval` cannot execute anything, and it accepts the
        # Python spellings (`True`, `None`, single quotes) that are not valid JSON.
        return ast.literal_eval(span)
    except (ValueError, SyntaxError):
        return _UNPARSEABLE


def _carries_plumbing(data: Any) -> bool:
    """True when any key, or any tool-name value, in this structure is internal plumbing."""
    if isinstance(data, dict):
        for key, value in data.items():
            token = str(key).strip().lower().replace("-", "_")
            if token in _TOOL_NAMES or token in _PAYLOAD_KEYS:
                return True
            if isinstance(value, str) and value.strip().lower() in _TOOL_NAMES:
                return True
            if isinstance(value, dict) and _carries_plumbing(value):
                return True
            if isinstance(value, list) and any(_carries_plumbing(item) for item in value):
                return True
        return False

    if isinstance(data, list):
        return any(_carries_plumbing(item) for item in data)

    return False


def _tidy(text: str) -> str:
    """Clean up what removing a span left behind: dead lines, blank runs, stray spacing.

    A line that is now only punctuation — or only the name of the tool whose payload was
    removed — says nothing to the customer, so it goes. That is also what leaves a reply
    that was *entirely* plumbing empty, which is how it reaches the approved fallback.
    """
    lines: list[str] = []
    for raw_line in text.splitlines():
        line = _SPACE_RUN.sub(" ", _SPACE_BEFORE_PUNCTUATION.sub(r"\1", raw_line.rstrip()))
        residue = _TOOL_NAMES_PATTERN.sub("", line).strip()
        if residue and not _PUNCTUATION_ONLY.match(residue):
            lines.append(line)
        elif lines and lines[-1] != "":
            lines.append("")

    while lines and lines[-1] == "":
        lines.pop()

    return _BLANK_RUN.sub("\n\n", "\n".join(lines)).strip()


__all__ = ["clean_customer_reply"]
