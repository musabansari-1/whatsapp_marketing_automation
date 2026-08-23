"""Qualification rules (specification §10, [D9], [D18], [D37]).

**The LLM does not decide qualification.** It extracts structured facts into
`leads.qualification_data`; this module applies explicit rules to those facts, and the
backend alone sets `lead.status`. That division is settled by [D9]; what is *not*
settled is the criteria.

[D37] leaves the `QUALIFYING → QUALIFIED` rules to a decision that has not been made,
and SPECIFICATION.md says they must not be invented earlier. So this module implements
the **mechanism** and takes the criteria from configuration:

    QUALIFICATION_REQUIRED_FIELDS   which extracted fields must be present

With that list empty — the default — `evaluate` never returns QUALIFIED. That is the
correct behaviour for an undecided rule: the machinery is complete and inert, and no
lead is qualified by accident. Supplying the fields turns it on without a code change.

§10's initial rule *concept* names three conditions: genuine interest, required
information collected, and demo intent or acceptance. The first and third are
judgements the extraction reports; the second is the configured field list. All three
are required here, so switching the rule on is one configuration change rather than a
new implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.domain.conversation import can_transition_lead
from app.domain.enums import LeadStatus

# Keys the extraction writes into `leads.qualification_data`. Named here so the schema
# the model is asked for and the schema the rules read cannot drift apart.
FIELD_INTERESTED = "interested"
FIELD_DEMO_INTENT = "demo_intent"
FIELD_NOT_INTERESTED = "not_interested"


@dataclass(frozen=True, slots=True)
class QualificationRules:
    """The configured criteria ([D37] pending).

    `required_fields` is empty by default, which makes qualification impossible on
    purpose — see the module docstring.
    """

    required_fields: frozenset[str] = frozenset()

    @property
    def is_configured(self) -> bool:
        """Whether the client's criteria have been supplied.

        Callers use this to explain why a lead is not qualifying, rather than
        reporting a rule failure that is really a missing decision.
        """
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


def rules_from_settings(required_fields: list[str]) -> QualificationRules:
    """Build the rules from configuration."""
    return QualificationRules(
        required_fields=frozenset(field.strip() for field in required_fields if field.strip())
    )


def missing_required_fields(data: dict[str, Any], rules: QualificationRules) -> frozenset[str]:
    """Which required fields the extraction has not filled.

    A field counts as present only when it holds a meaningful value: the model
    frequently emits `null` or `""` for something it did not learn, and treating that
    as collected would qualify a lead on absent information.
    """
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
    if _is_true(data.get(FIELD_NOT_INTERESTED)):
        if can_transition_lead(current, LeadStatus.NOT_INTERESTED):
            return QualificationResult(
                status=LeadStatus.NOT_INTERESTED,
                reasons=("the customer said they are not interested",),
            )
        return QualificationResult(status=None, reasons=("already past NOT_INTERESTED",))

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
        if can_transition_lead(current, LeadStatus.INTERESTED):
            return QualificationResult(
                status=LeadStatus.INTERESTED,
                missing_fields=missing,
                reasons=(*reasons, "the customer expressed interest"),
            )

    return QualificationResult(status=None, missing_fields=missing, reasons=tuple(reasons))


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list | dict | tuple | set):
        return bool(value)
    return True


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
    "FIELD_NOT_INTERESTED",
    "QualificationResult",
    "QualificationRules",
    "evaluate",
    "missing_required_fields",
    "rules_from_settings",
]
