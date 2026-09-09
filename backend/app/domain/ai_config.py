"""AI configuration resolution (§28.4, Phase 22).

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
to its customers and whom it treats as qualified. Neither may reach a product that did
not configure it: SustainX must not inherit Academicae's prompt merely because both use
the same model. A product without them fails closed rather than borrowing.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

T = TypeVar("T")


class AIConfigurationError(Exception):
    """A product has no usable AI policy, and none may be borrowed."""


class ConfigurationRow(Protocol):
    """The columns `ai_configurations` contributes. Every one nullable: NULL inherits."""

    system_prompt: str | None
    qualification_required_fields: list[Any] | None
    model: str | None
    rag_top_k: int | None
    rag_min_similarity: float | None
    rag_max_context_chars: int | None
    history_turns: int | None
    max_tool_rounds: int | None
    fallback_message: str | None


@dataclass(frozen=True, slots=True)
class PlatformDefaults:
    """Operational baselines, and the legacy policy the sole product may still use.

    `legacy_policy` carries the environment-configured prompt, criteria and fallback
    copy. It is only ever offered when `legacy_policy_allowed` is true — see
    `resolve`.
    """

    model: str | None
    rag_top_k: int
    rag_min_similarity: float
    rag_max_context_chars: int
    history_turns: int
    max_tool_rounds: int

    legacy_system_prompt: str
    legacy_qualification_required_fields: Sequence[str]
    legacy_fallback_message: str


@dataclass(frozen=True, slots=True)
class ResolvedAIConfig:
    """What one turn runs with, after inheritance."""

    system_prompt: str
    qualification_required_fields: tuple[str, ...]
    model: str | None
    rag_top_k: int
    rag_min_similarity: float
    rag_max_context_chars: int
    history_turns: int
    max_tool_rounds: int
    fallback_message: str

    #: True when the product's policy came from the platform's legacy environment
    #: configuration rather than from its own row. Transitional; see `resolve`.
    used_legacy_policy: bool = False


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
    legacy_policy_allowed: bool,
) -> ResolvedAIConfig:
    """Combine a segment override, a product default and the platform baseline.

    Precedence, per field: **segment, then product, then platform.**

    For operational fields the platform baseline is always available — they describe how
    the platform runs, not what a product says.

    For **policy** fields — the system prompt and the qualification criteria — the
    platform baseline is only reachable when `legacy_policy_allowed`. Those two decide
    what a product says and who it treats as qualified, and neither may reach a product
    that did not configure it. That flag is the
    transitional compatibility rule of §28.4: while exactly one active product exists and
    it has configured no AI row of its own, the environment-configured behaviour it has
    always used continues to apply, so [D54] holds.

    It is deliberately structural. It depends on the *count* of active products and
    never on which product it is — no slug, no name, no UUID, no "first". The moment a
    second active product exists the flag is false for everyone, and a product with no
    policy of its own raises rather than borrowing another's voice.

    Raises `AIConfigurationError` when a policy field cannot be resolved. The caller
    fails the turn closed rather than replying with configuration that belongs to someone
    else.
    """
    system_prompt = _first(
        segment.system_prompt if segment else None,
        product.system_prompt if product else None,
        defaults.legacy_system_prompt if legacy_policy_allowed else None,
    )
    if system_prompt is None:
        raise AIConfigurationError(
            "This product has no system prompt configured, and no other product's "
            "prompt may be used in its place."
        )

    # Platform-defaulted rather than gated, and the distinction is deliberate.
    #
    # This is the "I don't have verified information on that yet" safety net, not a
    # product's voice — generic infrastructure copy that Academicae happens to use too.
    # Withholding it would leave a correctly configured product unable to say even that,
    # while gaining nothing: a product that has not configured a *prompt* never reaches
    # here, because the check above already refused the turn.
    #
    # A product or segment that wants its own wording simply sets one.
    fallback_message = _first(
        segment.fallback_message if segment else None,
        product.fallback_message if product else None,
        defaults.legacy_fallback_message,
    )
    if fallback_message is None:  # pragma: no cover - the platform always supplies one
        raise AIConfigurationError("No fallback message is configured anywhere.")

    # NULL inherits; `[]` is a deliberate "no criteria", under which [D37] means nothing
    # can qualify. `_first` distinguishes them where `or` would not.
    #
    # Unset everywhere resolves to **no criteria** rather than raising, and that is the
    # fail-safe direction. The harm here is qualifying a lead on criteria nobody chose
    # for their product; declining to qualify anyone is merely inert, and is exactly what
    # the platform does today while [D37] is unresolved. A product that has configured no
    # prompt is already refused above, so this only affects one that has a voice but has
    # not yet said what "qualified" means to it.
    criteria = _first(
        segment.qualification_required_fields if segment else None,
        product.qualification_required_fields if product else None,
        list(defaults.legacy_qualification_required_fields) if legacy_policy_allowed else [],
    )

    resolved = ResolvedAIConfig(
        system_prompt=system_prompt,
        qualification_required_fields=tuple(str(field) for field in (criteria or [])),
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
        used_legacy_policy=legacy_policy_allowed
        and (segment is None or segment.system_prompt is None)
        and (product is None or product.system_prompt is None),
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


__all__ = [
    "AIConfigurationError",
    "ConfigurationRow",
    "PlatformDefaults",
    "ResolvedAIConfig",
    "resolve",
]
