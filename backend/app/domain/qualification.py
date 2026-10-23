"""Qualification rules (specification §10, [D9], [D18], [D37]).

**The LLM does not decide qualification.** It extracts structured facts into
`leads.qualification_data`; this module applies explicit rules to those facts, and the
backend alone sets `lead.status`. That division is settled by [D9].

Two modes are configured per product ([D37]):

    required_fields   which extracted fields must be present (legacy checklist)
    ai_judgement      no fixed checklist; the model records a judgement fact and the
                      interest / demo-intent signals, and this module maps them

With `required_fields` mode and an empty field list — the default — `evaluate` never
returns QUALIFIED. That keeps an undecided product inert. `ai_judgement` is an explicit
configuration that turns qualification on without inventing a field checklist.

Conceptual judgements the model may record (`qualification_judgement`) map onto the
existing §10 lead statuses — the platform does not add parallel status values:

    QUALIFIED            → QUALIFIED (via QUALIFYING when required by the table)
    NEEDS_MORE_CONTEXT   → QUALIFYING / INTERESTED
    NOT_QUALIFIED        → NOT_INTERESTED

Qualification never blocks booking: `book_demo` does not read these rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.domain.conversation import can_transition_lead
from app.domain.enums import LeadStatus

# Keys the extraction writes into `leads.qualification_data`. Named here so the schema
# the model is asked for and the schema the rules read cannot drift apart.
FIELD_INTERESTED = "interested"
FIELD_DEMO_INTENT = "demo_intent"
# The external-registration counterpart of demo intent: the customer wants to join or
# sign up. Life Herba's prompt records it as its strongest signal.
FIELD_REGISTRATION_INTENT = "registration_intent"
FIELD_NOT_INTERESTED = "not_interested"
# The customer asked to speak to or hear from a person. Not a qualification signal:
# it rings the team bell, because the assistant promises them a reply in the chat.
FIELD_HUMAN_REQUESTED = "human_requested"
FIELD_JUDGEMENT = "qualification_judgement"

JUDGEMENT_QUALIFIED = "QUALIFIED"
JUDGEMENT_NEEDS_MORE_CONTEXT = "NEEDS_MORE_CONTEXT"
JUDGEMENT_NOT_QUALIFIED = "NOT_QUALIFIED"


class QualificationMode(StrEnum):
    """How a product decides QUALIFYING → QUALIFIED ([D37])."""

    REQUIRED_FIELDS = "required_fields"
    AI_JUDGEMENT = "ai_judgement"


@dataclass(frozen=True, slots=True)
class QualificationRules:
    """The configured criteria ([D37]).

    In `required_fields` mode, an empty `required_fields` set makes qualification
    impossible on purpose. In `ai_judgement` mode the checklist is unused: interest,
    demo intent and an optional judgement fact drive the outcome.
    """

    required_fields: frozenset[str] = frozenset()
    mode: QualificationMode = QualificationMode.REQUIRED_FIELDS

    @property
    def is_configured(self) -> bool:
        """Whether criteria exist that can produce QUALIFIED.

        Callers use this to explain why a lead is not qualifying, rather than
        reporting a rule failure that is really a missing decision.
        """
        if self.mode is QualificationMode.AI_JUDGEMENT:
            return True
        return bool(self.required_fields)


@dataclass(frozen=True, slots=True)
class QualificationResult:
    """What the rules concluded, and why.

    `reasons` exists so a human reviewing a lead can see which condition failed. A
    bare boolean would make an unqualified lead indistinguishable from an unconfigured
    rule.
    """

    status: LeadStatus | None
    missing_fields: frozenset[str] = frozenset()
    reasons: tuple[str, ...] = ()

    @property
    def changes_status(self) -> bool:
        return self.status is not None


def rules_from_settings(
    required_fields: list[str],
    mode: str | QualificationMode | None = None,
) -> QualificationRules:
    """Build the rules from configuration."""
    resolved_mode = QualificationMode.REQUIRED_FIELDS
    if mode is not None and str(mode).strip():
        try:
            resolved_mode = QualificationMode(str(mode).strip())
        except ValueError as exc:
            raise ValueError(
                f"Unknown qualification mode {mode!r}; use {[m.value for m in QualificationMode]}"
            ) from exc

    return QualificationRules(
        required_fields=frozenset(field.strip() for field in required_fields if field.strip()),
        mode=resolved_mode,
    )


def missing_required_fields(data: dict[str, Any], rules: QualificationRules) -> frozenset[str]:
    """Which required fields the extraction has not filled.

    A field counts as present only when it holds a meaningful value: the model
    frequently emits `null` or `""` for something it did not learn, and treating that
    as collected would qualify a lead on absent information.

    AI-judgement mode has no checklist, so nothing is missing by this rule.
    """
    if rules.mode is QualificationMode.AI_JUDGEMENT:
        return frozenset()
    return frozenset(field for field in rules.required_fields if not _has_value(data.get(field)))


def evaluate(
    *,
    current: LeadStatus,
    data: dict[str, Any],
    rules: QualificationRules,
) -> QualificationResult:
    """Decide what a lead's status should become, given the extracted facts.

    Returns the target status, or `None` to leave it alone. Only ever proposes a
    transition the §10 table permits — the caller applies it through
    `transition_lead`, which checks again.

    Order matters: disinterest and demo intent are read before qualification, because
    a customer who has said no should not be walked further down the funnel by a rule
    about collected fields.
    """
    reasons: list[str] = []

    # An explicit "no" is the strongest signal in the conversation and outranks
    # everything else the extraction reports.
    if _is_true(data.get(FIELD_NOT_INTERESTED)) or _judgement(data) == JUDGEMENT_NOT_QUALIFIED:
        if can_transition_lead(current, LeadStatus.NOT_INTERESTED):
            return QualificationResult(
                status=LeadStatus.NOT_INTERESTED,
                reasons=("the customer is not a fit or said they are not interested",),
            )
        return QualificationResult(status=None, reasons=("already past NOT_INTERESTED",))

    if rules.mode is QualificationMode.AI_JUDGEMENT:
        return _evaluate_ai_judgement(current=current, data=data)

    missing = missing_required_fields(data, rules)

    if not rules.is_configured:
        # [D37]: no criteria have been supplied, so nothing can qualify. Said plainly
        # rather than reported as a rule failure, because it is a missing decision.
        reasons.append("qualification criteria are not configured ([D37])")
    elif missing:
        reasons.append(f"missing required information: {', '.join(sorted(missing))}")

    qualifies = (
        rules.is_configured
        and not missing
        and _is_true(data.get(FIELD_INTERESTED))
        and _is_true(data.get(FIELD_DEMO_INTENT))
    )

    if qualifies and can_transition_lead(current, LeadStatus.QUALIFIED):
        return QualificationResult(
            status=LeadStatus.QUALIFIED,
            reasons=("all configured criteria are met",),
        )

    if qualifies:
        # The rules are satisfied but §10 forbids the jump — usually because the lead
        # has not passed through QUALIFYING yet. Advancing there is the honest move.
        if can_transition_lead(current, LeadStatus.QUALIFYING):
            return QualificationResult(
                status=LeadStatus.QUALIFYING,
                reasons=("criteria met, but the lead must pass through QUALIFYING first",),
            )
        return QualificationResult(status=None, reasons=("no permitted transition",))

    # Not qualified. Interest alone is still progress worth recording, because it is
    # what a salesperson filters the dashboard by.
    if _is_true(data.get(FIELD_INTERESTED)):
        if missing and can_transition_lead(current, LeadStatus.QUALIFYING):
            return QualificationResult(
                status=LeadStatus.QUALIFYING,
                missing_fields=missing,
                reasons=(*reasons, "collecting the remaining information"),
            )
        # Never back out of QUALIFYING: the same facts would move it forward again on
        # the next turn, flipping the lead between the two states.
        if current is not LeadStatus.QUALIFYING and can_transition_lead(
            current, LeadStatus.INTERESTED
        ):
            return QualificationResult(
                status=LeadStatus.INTERESTED,
                missing_fields=missing,
                reasons=(*reasons, "the customer expressed interest"),
            )

    return QualificationResult(status=None, missing_fields=missing, reasons=tuple(reasons))


def _evaluate_ai_judgement(*, current: LeadStatus, data: dict[str, Any]) -> QualificationResult:
    """Map AI-extracted judgement / interest signals onto §10 statuses.

    A direct demo request (`demo_intent`) with interest is enough to qualify — booking
    must not wait on a rigid field checklist. An explicit QUALIFIED judgement does the
    same, and so does `registration_intent`: for an external-registration product,
    asking how to join is the conversion intent itself. Unclear intent stays in
    QUALIFYING / INTERESTED rather than being forced out.

    A lead already in QUALIFYING is never walked back to INTERESTED. The facts persist
    across turns, so stepping back made the next turn step forward again and the lead
    flipped between the two on every message.
    """
    judgement = _judgement(data)
    qualifies = (
        judgement == JUDGEMENT_QUALIFIED
        or _is_true(data.get(FIELD_REGISTRATION_INTENT))
        or (_is_true(data.get(FIELD_INTERESTED)) and _is_true(data.get(FIELD_DEMO_INTENT)))
    )

    if qualifies and can_transition_lead(current, LeadStatus.QUALIFIED):
        return QualificationResult(
            status=LeadStatus.QUALIFIED,
            reasons=("AI judgement mode: meaningful interest and demo intent",),
        )
    if qualifies and can_transition_lead(current, LeadStatus.QUALIFYING):
        return QualificationResult(
            status=LeadStatus.QUALIFYING,
            reasons=("criteria met, but the lead must pass through QUALIFYING first",),
        )

    needs_context = judgement == JUDGEMENT_NEEDS_MORE_CONTEXT or _is_true(
        data.get(FIELD_INTERESTED)
    )
    if needs_context and can_transition_lead(current, LeadStatus.QUALIFYING):
        return QualificationResult(
            status=LeadStatus.QUALIFYING,
            reasons=(
                "AI judgement mode: more context needed, or interest without demo intent",
            ),
        )
    if (
        needs_context
        and current is not LeadStatus.QUALIFYING
        and can_transition_lead(current, LeadStatus.INTERESTED)
    ):
        return QualificationResult(
            status=LeadStatus.INTERESTED,
            reasons=("AI judgement mode: the customer expressed interest",),
        )

    return QualificationResult(
        status=None,
        reasons=("AI judgement mode: no status change from the extracted facts",),
    )


def _judgement(data: dict[str, Any]) -> str | None:
    raw = data.get(FIELD_JUDGEMENT)
    if raw is None:
        return None
    text = str(raw).strip().upper().replace(" ", "_")
    if text in {JUDGEMENT_QUALIFIED, JUDGEMENT_NEEDS_MORE_CONTEXT, JUDGEMENT_NOT_QUALIFIED}:
        return text
    return None


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list | dict | tuple | set):
        return bool(value)
    return True


def is_affirmative(value: Any) -> bool:
    """The loose boolean reading the rules use, for callers outside this module."""
    return _is_true(value)


def _is_true(value: Any) -> bool:
    """Read a boolean the model may have expressed as a string.

    Models return `"yes"`, `"true"` and `True` interchangeably however firmly the
    schema asks for a boolean, and reading only `True` would silently drop half of
    them.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "y", "1"}
    return False


__all__ = [
    "FIELD_DEMO_INTENT",
    "FIELD_INTERESTED",
    "FIELD_JUDGEMENT",
    "FIELD_NOT_INTERESTED",
    "JUDGEMENT_NEEDS_MORE_CONTEXT",
    "JUDGEMENT_NOT_QUALIFIED",
    "JUDGEMENT_QUALIFIED",
    "QualificationMode",
    "QualificationResult",
    "QualificationRules",
    "evaluate",
    "missing_required_fields",
    "rules_from_settings",
]
