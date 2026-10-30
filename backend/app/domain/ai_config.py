"""AI configuration resolution (§28.4, Phases 22 and 27).

One product default, optional sparse segment overrides, resolved field by field. Pure
logic: the caller supplies the rows and the platform baseline, and this decides what the
turn actually runs with.

**Sparse means NULL is absence, not emptiness.** A segment row states only what differs
from its product, so every value column is nullable and NULL means inherit. Truthiness
merging would destroy that distinction, and for one field it destroys a real one:

    qualification_required_fields = NULL   inherit the product's criteria
    qualification_required_fields = []     deliberately none, so nothing can qualify

Those are different instructions and `[] or product_value` collapses them. Resolution here
is NULL-aware throughout, never truthiness-based.

**Two kinds of field, and the difference matters.**

*Operational* — model, retrieval limits, history depth, tool rounds, and the generic
no-knowledge fallback — describe how the platform runs. They fall back to platform
defaults, because they say nothing about a product's voice.

*Policy* — the system prompt and the qualification criteria — decide what a product says
to its customers and whom it treats as qualified. They come from the product's own rows
and from nowhere else. A product without a system prompt fails closed.

**There is no platform policy.** Until Phase 27 a transitional rule let the sole active
product fall back to the prompt and criteria in the environment, so the original product
kept working before it had rows of its own ([D54]). Phase 27 copies that configuration into
the product (`python -m app.cli import-legacy-config`) and deletes the rule. An active
product's voice is its own configuration, however many products exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from app.domain.qualification import QualificationMode

T = TypeVar("T")


class AIConfigurationError(Exception):
    """A product has no usable AI policy, and none may be borrowed."""


class ConfigurationRow(Protocol):
    """The columns `ai_configurations` contributes. Every one nullable: NULL inherits."""

    system_prompt: str | None
    qualification_required_fields: list[Any] | None
    qualification_mode: str | None
    model: str | None
    rag_top_k: int | None
    rag_min_similarity: float | None
    rag_max_context_chars: int | None
    history_turns: int | None
    max_tool_rounds: int | None
    fallback_message: str | None


@dataclass(frozen=True, slots=True)
class PlatformDefaults:
    """The operational baselines every product inherits.

    Deliberately no system prompt and no qualification criteria: those are a product's
    policy, and a platform value for them is exactly the fallback that would let one
    product speak with another's instructions.
    """

    model: str | None
    rag_top_k: int
    rag_min_similarity: float
    rag_max_context_chars: int
    history_turns: int
    max_tool_rounds: int
    fallback_message: str


@dataclass(frozen=True, slots=True)
class ResolvedAIConfig:
    """What one turn runs with, after inheritance."""

    system_prompt: str
    qualification_required_fields: tuple[str, ...]
    qualification_mode: QualificationMode
    model: str | None
    rag_top_k: int
    rag_min_similarity: float
    rag_max_context_chars: int
    history_turns: int
    max_tool_rounds: int
    fallback_message: str


def _first(*candidates: T | None) -> T | None:
    """The first value that is not NULL. Absence is NULL, never falsiness."""
    for candidate in candidates:
        if candidate is not None:
            return candidate
    return None


def resolve(
    *,
    segment: ConfigurationRow | None,
    product: ConfigurationRow | None,
    defaults: PlatformDefaults,
) -> ResolvedAIConfig:
    """Combine a segment override, a product default and the platform baseline.

    Precedence, per field: **segment, then product, then platform** — where the platform
    supplies operational values only.

    Raises `AIConfigurationError` when the system prompt cannot be resolved from the
    product's own rows, or when a resolved value is out of bounds. The caller fails the
    turn closed rather than replying with configuration that belongs to someone else.
    """
    system_prompt = _first(
        segment.system_prompt if segment else None,
        product.system_prompt if product else None,
    )
    if system_prompt is None:
        raise AIConfigurationError(
            "This product has no system prompt configured, and no other product's "
            "prompt may be used in its place."
        )

    # Platform-defaulted rather than refused, and the distinction is deliberate.
    #
    # This is the "I don't have verified information on that yet" safety net, not a
    # product's voice — generic infrastructure copy. Withholding it would leave a correctly
    # configured product unable to say even that, while gaining nothing: a product that has
    # not configured a *prompt* never reaches here, because the check above refused it.
    #
    # A product or segment that wants its own wording simply sets one.
    fallback_message = _first(
        segment.fallback_message if segment else None,
        product.fallback_message if product else None,
        defaults.fallback_message,
    )
    if fallback_message is None:  # pragma: no cover - the platform always supplies one
        raise AIConfigurationError("No fallback message is configured anywhere.")

    # NULL inherits; `[]` is a deliberate "no criteria", under which [D37] means nothing
    # can qualify in required_fields mode. `_first` distinguishes them where `or` would not.
    #
    # Unset everywhere resolves to **no criteria** rather than raising, and that is the
    # fail-safe direction. The harm here is qualifying a lead on criteria nobody chose for
    # their product; declining to qualify anyone is merely inert. Readiness reports it.
    # `ai_judgement` mode does not use the checklist — readiness treats that mode as
    # configured even when the list is empty.
    criteria = _first(
        segment.qualification_required_fields if segment else None,
        product.qualification_required_fields if product else None,
    )
    mode_raw = _first(
        segment.qualification_mode if segment else None,
        product.qualification_mode if product else None,
    )
    try:
        qualification_mode = (
            QualificationMode(mode_raw)
            if mode_raw is not None
            else QualificationMode.REQUIRED_FIELDS
        )
    except ValueError as exc:
        raise AIConfigurationError(
            f"Unknown qualification mode {mode_raw!r}; use {[m.value for m in QualificationMode]}"
        ) from exc

    resolved = ResolvedAIConfig(
        system_prompt=system_prompt,
        qualification_required_fields=tuple(str(field) for field in (criteria or [])),
        qualification_mode=qualification_mode,
        model=_first(
            segment.model if segment else None,
            product.model if product else None,
            defaults.model,
        ),
        rag_top_k=_require(
            _first(
                segment.rag_top_k if segment else None,
                product.rag_top_k if product else None,
                defaults.rag_top_k,
            )
        ),
        rag_min_similarity=_require(
            _first(
                segment.rag_min_similarity if segment else None,
                product.rag_min_similarity if product else None,
                defaults.rag_min_similarity,
            )
        ),
        rag_max_context_chars=_require(
            _first(
                segment.rag_max_context_chars if segment else None,
                product.rag_max_context_chars if product else None,
                defaults.rag_max_context_chars,
            )
        ),
        history_turns=_require(
            _first(
                segment.history_turns if segment else None,
                product.history_turns if product else None,
                defaults.history_turns,
            )
        ),
        max_tool_rounds=_require(
            _first(
                segment.max_tool_rounds if segment else None,
                product.max_tool_rounds if product else None,
                defaults.max_tool_rounds,
            )
        ),
        fallback_message=fallback_message,
    )

    _validate(resolved)
    return resolved


def _require(value: T | None) -> T:
    """An operational field always has a platform default, so this cannot be None."""
    if value is None:  # pragma: no cover - defended against, not reachable
        raise AIConfigurationError("An operational setting resolved to nothing")
    return value


def _validate(config: ResolvedAIConfig) -> None:
    """Keep the bounds that environment configuration used to enforce.

    Moving these values into the database must not mean nonsense is silently accepted:
    a `rag_top_k` of zero retrieves nothing and a negative history depth is meaningless,
    and both would previously have been refused at startup. Checked here because this is
    the boundary every turn passes through, whichever row supplied the value.
    """
    if not config.system_prompt.strip():
        raise AIConfigurationError("The system prompt is empty")
    if config.rag_top_k < 1:
        raise AIConfigurationError(f"rag_top_k must be at least 1, got {config.rag_top_k}")
    if not 0.0 <= config.rag_min_similarity <= 1.0:
        raise AIConfigurationError(
            f"rag_min_similarity must be between 0 and 1, got {config.rag_min_similarity}"
        )
    if config.rag_max_context_chars < 1:
        raise AIConfigurationError("rag_max_context_chars must be at least 1")
    if config.history_turns < 1:
        raise AIConfigurationError("history_turns must be at least 1")
    if config.max_tool_rounds < 1:
        raise AIConfigurationError("max_tool_rounds must be at least 1")


def with_audience_segment_context(
    system_prompt: str,
    *,
    segment_display_name: str | None,
) -> str:
    """Append who received the campaign, without implying what they want.

    The segment is marketing audience context. Interest must still be discovered from
    the conversation. Empty when the conversation has no segment.
    """
    name = (segment_display_name or "").strip()
    if not name:
        return system_prompt
    block = "\n".join(
        [
            "## Audience segment context",
            "",
            f"This conversation's marketing audience segment is: {name}.",
            "",
            "The segment tells you who received the campaign. It does NOT tell you what",
            "they are interested in. Discover their actual interest from what they say.",
            "Do not presume, prioritize, or redirect toward a need based on audience type alone.",
        ]
    )
    prompt = system_prompt.rstrip()
    return f"{prompt}\n\n{block}" if prompt else block


def with_campaign_context(
    system_prompt: str,
    *,
    template_name: str | None,
    body: str | None,
) -> str:
    """Append the last campaign message this customer was sent.

    A button tap arrives as bare text ("I'm Interested"), and several templates share
    the same button labels, so without the message it answers the model cannot tell
    which offer the customer means. Nothing is appended when no campaign reached them.
    """
    text = (body or "").strip()
    if not text:
        return system_prompt
    lines = ["## Last campaign message this customer received", ""]
    if template_name:
        lines.append(f"Template: {template_name}")
        lines.append("")
    lines.extend(
        [
            text,
            "",
            "The customer's latest replies answer this message. A short reply such as a",
            "button tap means this offer. When the knowledge base has a script for the",
            "button or reply, send that script's message word for word, then ask its",
            "follow-up questions one at a time; do not summarise the offer instead. The",
            "scripts take priority over any registration call to action and over topics",
            "from earlier conversations.",
        ]
    )
    prompt = system_prompt.rstrip()
    block = "\n".join(lines)
    return f"{prompt}\n\n{block}" if prompt else block


def with_lead_context(
    system_prompt: str,
    *,
    full_name: str | None,
    phone_e164: str,
    email: str | None,
    company: str | None,
    designation: str | None,
    city: str | None,
    status: str,
    known_facts: dict[str, object],
) -> str:
    """Append what is already known about this customer.

    The backend loaded the lead before the turn, so handing the model its summary
    saves the `get_lead` round most turns spend just to learn a name and an email.
    The tool stays for anything beyond what is listed here. Mirrors the tool's own
    shape (name/email/company/designation/city/status/known facts) so the two
    sources can never disagree about what "known" means. Empty values are omitted;
    facts render sorted so the block is stable turn to turn.
    """
    lines = ["## Customer record (already known)", ""]
    name = (full_name or "").strip()
    if name:
        lines.append(f"- Name: {name}")
    lines.append(f"- Phone: {phone_e164}")
    email_address = (email or "").strip()
    if email_address:
        lines.append(f"- Email: {email_address}")
    employer = (company or "").strip()
    if employer:
        lines.append(f"- Company: {employer}")
    role = (designation or "").strip()
    if role:
        lines.append(f"- Designation: {role}")
    town = (city or "").strip()
    if town:
        lines.append(f"- City: {town}")
    lines.append(f"- Status: {status}")
    facts = {key: value for key, value in known_facts.items() if value is not None}
    if facts:
        rendered = ", ".join(f"{key}={value}" for key, value in sorted(facts.items()))
        lines.append(f"- Known facts: {rendered}")
    lines.extend(
        [
            "",
            "These details are already known — do not re-ask for them. Call get_lead",
            "only if you need more than what is listed here.",
        ]
    )
    prompt = system_prompt.rstrip()
    block = "\n".join(lines)
    return f"{prompt}\n\n{block}" if prompt else block


__all__ = [
    "AIConfigurationError",
    "ConfigurationRow",
    "PlatformDefaults",
    "ResolvedAIConfig",
    "resolve",
    "with_audience_segment_context",
    "with_lead_context",
]
