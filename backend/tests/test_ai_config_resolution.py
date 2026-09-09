"""AI configuration inheritance (Phase 22, §28.4).

Product default, optional sparse segment override, resolved field by field. Pure logic,
so the rule can be stated exactly and tested without a database.

Two shapes have to work from one mechanism, with no product-specific branching anywhere:

    SustainX-like    one product row, several segments, zero overrides
    Life Herba-like  one product row plus sparse per-segment overrides
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from app.domain.ai_config import (
    AIConfigurationError,
    PlatformDefaults,
    resolve,
)


@dataclass
class Row:
    """An `ai_configurations` row. Every field NULL by default: NULL means inherit."""

    system_prompt: str | None = None
    qualification_required_fields: list[Any] | None = None
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
    legacy_system_prompt="Legacy platform prompt.",
    legacy_qualification_required_fields=["legacy_field"],
    legacy_fallback_message="Legacy fallback.",
)


class TestPrecedence:
    def test_a_segment_value_wins_over_the_product(self) -> None:
        resolved = resolve(
            segment=Row(system_prompt="Segment prompt."),
            product=Row(system_prompt="Product prompt."),
            defaults=DEFAULTS,
            legacy_policy_allowed=False,
        )
        assert resolved.system_prompt == "Segment prompt."

    def test_a_product_value_is_used_when_the_segment_is_silent(self) -> None:
        resolved = resolve(
            segment=Row(),
            product=Row(system_prompt="Product prompt."),
            defaults=DEFAULTS,
            legacy_policy_allowed=False,
        )
        assert resolved.system_prompt == "Product prompt."

    def test_operational_fields_fall_back_to_the_platform(self) -> None:
        """Model, retrieval limits, history and tool rounds describe how the platform
        runs, not what a product says, so a platform default is legitimate."""
        resolved = resolve(
            segment=None,
            product=Row(system_prompt="Product prompt."),
            defaults=DEFAULTS,
            legacy_policy_allowed=False,
        )
        assert resolved.rag_top_k == 5
        assert resolved.history_turns == 20
        assert resolved.max_tool_rounds == 4
        assert resolved.model == "platform/model"

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
            legacy_policy_allowed=False,
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
            legacy_policy_allowed=False,
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
            legacy_policy_allowed=False,
        )
        assert resolved.qualification_required_fields == ()

    def test_an_empty_product_list_is_not_replaced_by_the_legacy_baseline(self) -> None:
        resolved = resolve(
            segment=None,
            product=Row(system_prompt="p", qualification_required_fields=[], fallback_message="f"),
            defaults=DEFAULTS,
            legacy_policy_allowed=True,
        )
        assert resolved.qualification_required_fields == ()


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

        government = resolve(
            segment=None, product=product, defaults=DEFAULTS, legacy_policy_allowed=False
        )
        industry = resolve(
            segment=None, product=product, defaults=DEFAULTS, legacy_policy_allowed=False
        )

        assert government == industry
        assert government.system_prompt == "Book a presentation with the team."

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

        as_pharmacy = resolve(
            segment=pharmacy, product=product, defaults=DEFAULTS, legacy_policy_allowed=False
        )
        as_consumer = resolve(
            segment=consumer, product=product, defaults=DEFAULTS, legacy_policy_allowed=False
        )
        as_shared = resolve(
            segment=None, product=product, defaults=DEFAULTS, legacy_policy_allowed=False
        )

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
    def test_a_product_without_policy_raises_rather_than_borrowing(self) -> None:
        """The rule that keeps SustainX from speaking in Academicae's voice.

        With more than one active product the legacy baseline is unavailable to
        everyone, so a product that configured nothing has nothing to say — and saying
        nothing is the correct outcome.
        """
        with pytest.raises(AIConfigurationError, match="system prompt"):
            resolve(segment=None, product=None, defaults=DEFAULTS, legacy_policy_allowed=False)

    def test_a_product_row_without_a_prompt_also_raises(self) -> None:
        with pytest.raises(AIConfigurationError, match="system prompt"):
            resolve(
                segment=None,
                product=Row(rag_top_k=9),
                defaults=DEFAULTS,
                legacy_policy_allowed=False,
            )

    def test_operational_defaults_still_apply_without_policy(self) -> None:
        """Platform operational values are not policy and are never withheld — the
        failure above is about voice, not about retrieval limits."""
        resolved = resolve(
            segment=None,
            product=Row(system_prompt="p", qualification_required_fields=[], fallback_message="f"),
            defaults=DEFAULTS,
            legacy_policy_allowed=False,
        )
        assert resolved.rag_top_k == 5


class TestLegacyCompatibility:
    def test_the_sole_product_may_use_the_platform_policy(self) -> None:
        """[D54]: while one product is active and has configured nothing, it keeps
        behaving exactly as it did before products existed."""
        resolved = resolve(
            segment=None, product=None, defaults=DEFAULTS, legacy_policy_allowed=True
        )
        assert resolved.system_prompt == "Legacy platform prompt."
        assert resolved.qualification_required_fields == ("legacy_field",)
        assert resolved.fallback_message == "Legacy fallback."
        assert resolved.used_legacy_policy is True

    def test_a_configured_product_does_not_use_it(self) -> None:
        resolved = resolve(
            segment=None,
            product=Row(system_prompt="Own prompt.", qualification_required_fields=["own"]),
            defaults=DEFAULTS,
            legacy_policy_allowed=True,
        )
        assert resolved.system_prompt == "Own prompt."
        assert resolved.used_legacy_policy is False


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
                legacy_policy_allowed=True,
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
                legacy_policy_allowed=True,
            )

    def test_a_blank_system_prompt_is_refused(self) -> None:
        with pytest.raises(AIConfigurationError, match="empty"):
            resolve(
                segment=None,
                product=Row(system_prompt="   ", qualification_required_fields=[]),
                defaults=DEFAULTS,
                legacy_policy_allowed=True,
            )
