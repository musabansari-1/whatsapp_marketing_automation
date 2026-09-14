"""Qualification rule tests (specification §10, [D9], [D18], [D37]).

The point of these is the division of responsibility: the model's extraction is input,
never a verdict. The criteria themselves are configuration and are deliberately not
asserted here — [D37] has not decided them.
"""

from __future__ import annotations

import pytest

from app.domain.enums import LeadStatus
from app.domain.qualification import (
    FIELD_DEMO_INTENT,
    FIELD_INTERESTED,
    FIELD_JUDGEMENT,
    FIELD_NOT_INTERESTED,
    JUDGEMENT_NEEDS_MORE_CONTEXT,
    JUDGEMENT_NOT_QUALIFIED,
    JUDGEMENT_QUALIFIED,
    QualificationMode,
    QualificationRules,
    evaluate,
    missing_required_fields,
    rules_from_settings,
)

CONFIGURED = QualificationRules(required_fields=frozenset({"team_size", "use_case"}))
UNCONFIGURED = QualificationRules()

COMPLETE = {
    FIELD_INTERESTED: True,
    FIELD_DEMO_INTENT: True,
    "team_size": 40,
    "use_case": "customer support",
}


class TestRulesFromSettings:
    def test_fields_come_from_configuration(self) -> None:
        assert rules_from_settings(["team_size", "budget"]).required_fields == frozenset(
            {"team_size", "budget"}
        )

    def test_blank_entries_are_ignored(self) -> None:
        """A trailing comma in configuration must not create a field nobody can fill."""
        assert rules_from_settings(["team_size", "  ", ""]).required_fields == frozenset(
            {"team_size"}
        )

    def test_no_configuration_means_no_criteria(self) -> None:
        assert not rules_from_settings([]).is_configured


class TestMissingFields:
    def test_present_fields_are_not_missing(self) -> None:
        assert missing_required_fields(COMPLETE, CONFIGURED) == frozenset()

    def test_absent_fields_are_missing(self) -> None:
        assert missing_required_fields({"team_size": 40}, CONFIGURED) == frozenset({"use_case"})

    @pytest.mark.parametrize("empty", [None, "", "   ", [], {}])
    def test_an_empty_value_does_not_count_as_collected(self, empty: object) -> None:
        """Models emit null and "" for things they did not learn. Treating those as
        collected would qualify a lead on absent information."""
        data = {**COMPLETE, "use_case": empty}
        assert "use_case" in missing_required_fields(data, CONFIGURED)

    def test_zero_counts_as_a_value(self) -> None:
        """0 is a real answer to "how many", and dropping it would ask again forever."""
        assert missing_required_fields({**COMPLETE, "team_size": 0}, CONFIGURED) == frozenset()


class TestUnconfiguredCriteria:
    """[D37] has not been decided, so nothing may qualify yet."""

    def test_a_lead_with_everything_still_does_not_qualify(self) -> None:
        result = evaluate(current=LeadStatus.QUALIFYING, data=COMPLETE, rules=UNCONFIGURED)
        assert result.status is not LeadStatus.QUALIFIED

    def test_the_reason_says_so_plainly(self) -> None:
        """A missing decision must not read as a rule failure — otherwise nobody knows
        the feature is switched off rather than working."""
        result = evaluate(current=LeadStatus.QUALIFYING, data=COMPLETE, rules=UNCONFIGURED)
        assert any("not configured" in reason for reason in result.reasons)

    def test_interest_is_still_recorded(self) -> None:
        """Interest is what a salesperson filters the dashboard by, and it does not
        depend on the undecided criteria."""
        result = evaluate(
            current=LeadStatus.RESPONDED, data={FIELD_INTERESTED: True}, rules=UNCONFIGURED
        )
        assert result.status is LeadStatus.INTERESTED


class TestConfiguredCriteria:
    def test_a_complete_lead_qualifies(self) -> None:
        result = evaluate(current=LeadStatus.QUALIFYING, data=COMPLETE, rules=CONFIGURED)
        assert result.status is LeadStatus.QUALIFIED

    def test_missing_information_does_not_qualify(self) -> None:
        data = {FIELD_INTERESTED: True, FIELD_DEMO_INTENT: True, "team_size": 40}
        result = evaluate(current=LeadStatus.QUALIFYING, data=data, rules=CONFIGURED)

        assert result.status is not LeadStatus.QUALIFIED
        assert result.missing_fields == frozenset({"use_case"})

    def test_no_demo_intent_does_not_qualify(self) -> None:
        """§10's rule concept requires demo intent, not just collected fields."""
        data = {**COMPLETE, FIELD_DEMO_INTENT: False}
        assert (
            evaluate(current=LeadStatus.QUALIFYING, data=data, rules=CONFIGURED).status
            is not LeadStatus.QUALIFIED
        )

    def test_no_interest_does_not_qualify(self) -> None:
        data = {**COMPLETE, FIELD_INTERESTED: False}
        assert (
            evaluate(current=LeadStatus.QUALIFYING, data=data, rules=CONFIGURED).status
            is not LeadStatus.QUALIFIED
        )

    def test_a_lead_that_has_not_reached_qualifying_goes_there_first(self) -> None:
        """§10 has no NEW → QUALIFIED edge. Advancing one step is honest; jumping
        would be the model's verdict overriding the state machine."""
        result = evaluate(current=LeadStatus.RESPONDED, data=COMPLETE, rules=CONFIGURED)
        assert result.status is LeadStatus.QUALIFYING

    def test_a_lead_still_collecting_moves_to_qualifying(self) -> None:
        data = {FIELD_INTERESTED: True, "team_size": 40}
        result = evaluate(current=LeadStatus.RESPONDED, data=data, rules=CONFIGURED)

        assert result.status is LeadStatus.QUALIFYING
        assert result.missing_fields == frozenset({"use_case"})


class TestDisinterest:
    def test_an_explicit_no_outranks_everything(self) -> None:
        """The strongest signal in the conversation. A customer who has said no must
        not be walked further down the funnel by a rule about collected fields."""
        data = {**COMPLETE, FIELD_NOT_INTERESTED: True}
        result = evaluate(current=LeadStatus.QUALIFYING, data=data, rules=CONFIGURED)

        assert result.status is LeadStatus.NOT_INTERESTED

    def test_it_applies_even_without_configured_criteria(self) -> None:
        result = evaluate(
            current=LeadStatus.RESPONDED,
            data={FIELD_NOT_INTERESTED: True},
            rules=UNCONFIGURED,
        )
        assert result.status is LeadStatus.NOT_INTERESTED


class TestStateMachineIsRespected:
    def test_nothing_is_proposed_from_a_terminal_state(self) -> None:
        """An opted-out lead is not moved by any extraction, however enthusiastic."""
        result = evaluate(current=LeadStatus.OPTED_OUT, data=COMPLETE, rules=CONFIGURED)
        assert result.status is None

    def test_only_permitted_transitions_are_proposed(self) -> None:
        """The caller applies the result through `transition_lead`, which checks again
        — but proposing an impossible move would guarantee a rejection every turn."""
        from app.domain.conversation import can_transition_lead

        for current in LeadStatus:
            for data in (COMPLETE, {FIELD_NOT_INTERESTED: True}, {FIELD_INTERESTED: True}):
                result = evaluate(current=current, data=data, rules=CONFIGURED)
                if result.status is not None:
                    assert can_transition_lead(current, result.status), (current, result.status)


class TestLooseBooleans:
    @pytest.mark.parametrize("value", [True, "true", "TRUE", "yes", "Y", "1"])
    def test_affirmatives_are_understood(self, value: object) -> None:
        """Models return these interchangeably however firmly the schema asks for a
        boolean, and reading only `True` would silently drop half of them."""
        data = {**COMPLETE, FIELD_INTERESTED: value}
        assert evaluate(current=LeadStatus.QUALIFYING, data=data, rules=CONFIGURED).status is (
            LeadStatus.QUALIFIED
        )

    @pytest.mark.parametrize("value", [False, "false", "no", "", None, "maybe"])
    def test_anything_else_is_not_affirmative(self, value: object) -> None:
        """ "maybe" is not consent to a demo, and treating it as one would qualify a
        lead who never agreed."""
        data = {**COMPLETE, FIELD_DEMO_INTENT: value}
        assert (
            evaluate(current=LeadStatus.QUALIFYING, data=data, rules=CONFIGURED).status
            is not LeadStatus.QUALIFIED
        )


class TestAIJudgementMode:
    """[D37] ai_judgement: no field checklist; interest / demo intent / judgement map."""

    AI = QualificationRules(mode=QualificationMode.AI_JUDGEMENT)

    def test_empty_required_fields_are_still_configured(self) -> None:
        assert self.AI.is_configured
        assert rules_from_settings([], mode="ai_judgement").is_configured
        assert rules_from_settings([], mode="ai_judgement").mode is QualificationMode.AI_JUDGEMENT

    def test_interested_and_demo_intent_qualify(self) -> None:
        data = {FIELD_INTERESTED: True, FIELD_DEMO_INTENT: True}
        result = evaluate(current=LeadStatus.QUALIFYING, data=data, rules=self.AI)
        assert result.status is LeadStatus.QUALIFIED

    def test_judgement_qualified_qualifies(self) -> None:
        data = {FIELD_JUDGEMENT: JUDGEMENT_QUALIFIED}
        result = evaluate(current=LeadStatus.QUALIFYING, data=data, rules=self.AI)
        assert result.status is LeadStatus.QUALIFIED

    def test_needs_more_context_stays_in_qualifying_or_interested(self) -> None:
        data = {FIELD_JUDGEMENT: JUDGEMENT_NEEDS_MORE_CONTEXT}

        from_responded = evaluate(current=LeadStatus.RESPONDED, data=data, rules=self.AI)
        assert from_responded.status is LeadStatus.QUALIFYING

        from_qualifying = evaluate(current=LeadStatus.QUALIFYING, data=data, rules=self.AI)
        assert from_qualifying.status is LeadStatus.INTERESTED

    def test_not_qualified_maps_to_not_interested(self) -> None:
        data = {FIELD_JUDGEMENT: JUDGEMENT_NOT_QUALIFIED}
        result = evaluate(current=LeadStatus.QUALIFYING, data=data, rules=self.AI)
        assert result.status is LeadStatus.NOT_INTERESTED

    def test_empty_required_fields_without_ai_judgement_still_do_not_qualify(self) -> None:
        data = {FIELD_INTERESTED: True, FIELD_DEMO_INTENT: True}
        result = evaluate(
            current=LeadStatus.QUALIFYING,
            data=data,
            rules=QualificationRules(),
        )
        assert result.status is not LeadStatus.QUALIFIED
        assert not rules_from_settings([]).is_configured
