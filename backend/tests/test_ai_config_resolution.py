"""AI configuration inheritance (Phases 22 and 27, §28.4).

Product default, optional sparse segment override, resolved field by field. Pure logic,
so the rule can be stated exactly and tested without a database.

Two shapes have to work from one mechanism, with no product-specific branching anywhere:

    SustainX-like    one product row, several segments, zero overrides
    Life Herba-like  one product row plus sparse per-segment overrides

And since Phase 27 there is no platform policy at all: a product's prompt and criteria come
from its own rows, whether one product exists or three.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any

import pytest

from app.domain.ai_config import (
    AIConfigurationError,
    PlatformDefaults,
    resolve,
    with_audience_segment_context,
    with_lead_context,
)
from app.domain.qualification import QualificationMode


@dataclass
class Row:
    """An `ai_configurations` row. Every field NULL by default: NULL means inherit."""

    system_prompt: str | None = None
    qualification_required_fields: list[Any] | None = None
    qualification_mode: str | None = None
    model: str | None = None
    rag_top_k: int | None = None
    rag_min_similarity: float | None = None
    rag_max_context_chars: int | None = None
    history_turns: int | None = None
    max_tool_rounds: int | None = None
    fallback_message: str | None = None


DEFAULTS = PlatformDefaults(
    model="platform/model",
    rag_top_k=5,
    rag_min_similarity=0.25,
    rag_max_context_chars=4000,
    history_turns=20,
    max_tool_rounds=4,
    fallback_message="Platform fallback.",
)


class TestPrecedence:
    def test_a_segment_value_wins_over_the_product(self) -> None:
        resolved = resolve(
            segment=Row(system_prompt="Segment prompt."),
            product=Row(system_prompt="Product prompt."),
            defaults=DEFAULTS,
        )
        assert resolved.system_prompt == "Segment prompt."

    def test_a_product_value_is_used_when_the_segment_is_silent(self) -> None:
        resolved = resolve(
            segment=Row(),
            product=Row(system_prompt="Product prompt."),
            defaults=DEFAULTS,
        )
        assert resolved.system_prompt == "Product prompt."

    def test_operational_fields_fall_back_to_the_platform(self) -> None:
        """Model, retrieval limits, history and tool rounds describe how the platform
        runs, not what a product says, so a platform default is legitimate."""
        resolved = resolve(
            segment=None, product=Row(system_prompt="Product prompt."), defaults=DEFAULTS
        )
        assert resolved.rag_top_k == 5
        assert resolved.history_turns == 20
        assert resolved.max_tool_rounds == 4
        assert resolved.model == "platform/model"
        assert resolved.fallback_message == "Platform fallback."

    def test_a_sparse_override_changes_only_what_it_states(self) -> None:
        """The Life Herba shape: a pharmacist prompt without restating everything."""
        resolved = resolve(
            segment=Row(system_prompt="Pharmacy focus."),
            product=Row(
                system_prompt="Ecosystem-wide.",
                qualification_required_fields=["interested"],
                rag_top_k=8,
            ),
            defaults=DEFAULTS,
        )
        assert resolved.system_prompt == "Pharmacy focus."
        assert resolved.qualification_required_fields == ("interested",)
        assert resolved.rag_top_k == 8


class TestNullVersusEmpty:
    """The distinction truthiness merging destroys."""

    def test_null_qualification_fields_inherit(self) -> None:
        resolved = resolve(
            segment=Row(qualification_required_fields=None),
            product=Row(system_prompt="p", qualification_required_fields=["interested", "budget"]),
            defaults=DEFAULTS,
        )
        assert resolved.qualification_required_fields == ("interested", "budget")

    def test_an_explicit_empty_list_does_not_inherit(self) -> None:
        """`[]` is a deliberate instruction: no criteria, so under [D37] nothing can
        qualify. `[] or product_value` would silently return the product's criteria and
        start qualifying leads the segment said should not be."""
        resolved = resolve(
            segment=Row(qualification_required_fields=[]),
            product=Row(system_prompt="p", qualification_required_fields=["interested", "budget"]),
            defaults=DEFAULTS,
        )
        assert resolved.qualification_required_fields == ()

    def test_criteria_unset_everywhere_mean_none(self) -> None:
        """The fail-safe direction: nobody qualifies on criteria nobody chose."""
        resolved = resolve(segment=None, product=Row(system_prompt="p"), defaults=DEFAULTS)
        assert resolved.qualification_required_fields == ()
        assert resolved.qualification_mode is QualificationMode.REQUIRED_FIELDS

    def test_ai_judgement_mode_resolves(self) -> None:
        resolved = resolve(
            segment=None,
            product=Row(
                system_prompt="p",
                qualification_mode="ai_judgement",
                qualification_required_fields=[],
            ),
            defaults=DEFAULTS,
        )
        assert resolved.qualification_mode is QualificationMode.AI_JUDGEMENT
        assert resolved.qualification_required_fields == ()

    def test_segment_mode_wins_over_the_product(self) -> None:
        resolved = resolve(
            segment=Row(qualification_mode="ai_judgement"),
            product=Row(system_prompt="p", qualification_mode="required_fields"),
            defaults=DEFAULTS,
        )
        assert resolved.qualification_mode is QualificationMode.AI_JUDGEMENT


class TestProductShapes:
    def test_the_sustainx_shape_resolves_identically_for_every_segment(self) -> None:
        """One product row, several segments, **zero** override rows.

        Every segment resolves to the same behaviour, which is the requirement — and it
        falls out of the general rule rather than any product-specific code.
        """
        product = Row(
            system_prompt="Book a presentation with the team.",
            qualification_required_fields=["interested"],
            fallback_message="A colleague will follow up.",
        )

        industries = resolve(segment=None, product=product, defaults=DEFAULTS)
        students = resolve(segment=None, product=product, defaults=DEFAULTS)

        assert industries == students
        assert industries.system_prompt == "Book a presentation with the team."


class TestAudienceSegmentContext:
    def test_appends_who_received_the_campaign_without_presuming_need(self) -> None:
        text = with_audience_segment_context(
            "Base prompt.",
            segment_display_name="Industries",
        )
        assert text.startswith("Base prompt.")
        assert "Industries" in text
        assert "does NOT tell you what" in text
        assert "Do not presume" in text

    def test_omits_block_when_there_is_no_segment(self) -> None:
        assert with_audience_segment_context("Base.", segment_display_name=None) == "Base."
        assert with_audience_segment_context("Base.", segment_display_name="  ") == "Base."


class TestLeadContext:
    """The lead summary riding along saves the `get_lead` round most turns spend just
    to learn a name and an email. Same shape as the tool's own record, so the two
    sources cannot disagree about what "known" means."""

    def _lead(self, **overrides: Any) -> Any:
        fields: dict[str, Any] = {
            "full_name": "Asha Rao",
            "phone_e164": "+919876543210",
            "email": "asha@example.com",
            "company": "Northwind",
            "designation": "Principal",
            "city": "Pune",
            "status": "QUALIFIED",
            "known_facts": {"interested": True, "demo_intent": True},
        }
        fields.update(overrides)
        return fields

    def test_appends_every_known_detail(self) -> None:
        text = with_lead_context("Base prompt.", **self._lead())

        assert text.startswith("Base prompt.")
        for expected in (
            "- Name: Asha Rao",
            "- Phone: +919876543210",
            "- Email: asha@example.com",
            "- Company: Northwind",
            "- Status: QUALIFIED",
            "- Known facts: demo_intent=True, interested=True",
            "Call get_lead",
        ):
            assert expected in text, expected

    def test_omits_empty_values_but_keeps_phone_and_status(self) -> None:
        text = with_lead_context(
            "Base.",
            **self._lead(
                full_name="  ",
                email=None,
                company=None,
                designation=None,
                city=None,
                known_facts={},
            ),
        )

        assert "- Phone: +919876543210" in text
        assert "- Status: QUALIFIED" in text
        assert "Name:" not in text
        assert "Email:" not in text
        assert "Known facts:" not in text

    def test_the_life_herba_shape_differs_only_where_stated(self) -> None:
        product = Row(
            system_prompt="Answer about the whole ecosystem.",
            qualification_required_fields=["interested"],
            fallback_message="A colleague will follow up.",
            rag_top_k=6,
        )
        pharmacy = Row(
            system_prompt="Focus on pharmacy onboarding.",
            qualification_required_fields=["interested", "pharmacy_licence"],
        )
        consumer = Row(system_prompt="Focus on consumer services.")

        as_pharmacy = resolve(segment=pharmacy, product=product, defaults=DEFAULTS)
        as_consumer = resolve(segment=consumer, product=product, defaults=DEFAULTS)
        as_shared = resolve(segment=None, product=product, defaults=DEFAULTS)

        assert as_pharmacy.system_prompt == "Focus on pharmacy onboarding."
        assert as_pharmacy.qualification_required_fields == ("interested", "pharmacy_licence")

        assert as_consumer.system_prompt == "Focus on consumer services."
        # Not overridden, so inherited — the point of sparseness.
        assert as_consumer.qualification_required_fields == ("interested",)

        assert as_shared.system_prompt == "Answer about the whole ecosystem."

        # Everything unstated is the product's, for all three.
        assert as_pharmacy.rag_top_k == as_consumer.rag_top_k == as_shared.rag_top_k == 6
        assert as_pharmacy.fallback_message == "A colleague will follow up."


class TestPolicyNeverCrossesProducts:
    def test_two_configured_products_each_resolve_their_own_policy(self) -> None:
        """Academicae and SustainX both configured: each speaks with its own prompt and
        qualifies on its own criteria, and neither sees the other's."""
        academicae = Row(system_prompt="Academicae's prompt.", qualification_required_fields=["a"])
        sustainx = Row(system_prompt="SustainX's prompt.", qualification_required_fields=["s"])

        first = resolve(segment=None, product=academicae, defaults=DEFAULTS)
        second = resolve(segment=None, product=sustainx, defaults=DEFAULTS)

        assert (first.system_prompt, first.qualification_required_fields) == (
            "Academicae's prompt.",
            ("a",),
        )
        assert (second.system_prompt, second.qualification_required_fields) == (
            "SustainX's prompt.",
            ("s",),
        )

    def test_a_product_without_policy_raises_rather_than_borrowing(self) -> None:
        """The rule that keeps SustainX from speaking in Academicae's voice. There is no
        platform policy to fall back to, however many products are active."""
        with pytest.raises(AIConfigurationError, match="system prompt"):
            resolve(segment=None, product=None, defaults=DEFAULTS)

    def test_a_product_row_without_a_prompt_also_raises(self) -> None:
        with pytest.raises(AIConfigurationError, match="system prompt"):
            resolve(segment=None, product=Row(rag_top_k=9), defaults=DEFAULTS)

    def test_the_platform_offers_no_policy_to_borrow(self) -> None:
        """Phase 27 deleted the legacy prompt and criteria from the platform defaults.
        What remains is operational only, so a policy fallback cannot be reintroduced by
        passing a value that happens to be there."""
        fields = {field.name for field in dataclasses.fields(PlatformDefaults)}
        assert not {name for name in fields if "prompt" in name or "qualification" in name}

    def test_operational_defaults_still_apply_without_policy_of_the_platform(self) -> None:
        resolved = resolve(
            segment=None,
            product=Row(system_prompt="p", qualification_required_fields=[]),
            defaults=DEFAULTS,
        )
        assert resolved.rag_top_k == 5


class TestValidation:
    """Bounds environment configuration used to enforce must survive the move."""

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("rag_top_k", 0),
            ("rag_max_context_chars", 0),
            ("history_turns", 0),
            ("max_tool_rounds", 0),
        ],
    )
    def test_non_positive_limits_are_refused(self, field: str, value: int) -> None:
        with pytest.raises(AIConfigurationError):
            resolve(
                segment=None,
                product=Row(
                    system_prompt="p",
                    qualification_required_fields=[],
                    **{field: value},  # type: ignore[arg-type]
                ),
                defaults=DEFAULTS,
            )

    @pytest.mark.parametrize("value", [-0.1, 1.1])
    def test_similarity_outside_zero_to_one_is_refused(self, value: float) -> None:
        with pytest.raises(AIConfigurationError, match="rag_min_similarity"):
            resolve(
                segment=None,
                product=Row(
                    system_prompt="p", qualification_required_fields=[], rag_min_similarity=value
                ),
                defaults=DEFAULTS,
            )

    def test_a_blank_system_prompt_is_refused(self) -> None:
        with pytest.raises(AIConfigurationError, match="empty"):
            resolve(
                segment=None,
                product=Row(system_prompt="   ", qualification_required_fields=[]),
                defaults=DEFAULTS,
            )
