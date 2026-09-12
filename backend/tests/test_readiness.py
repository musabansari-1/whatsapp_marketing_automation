"""Product readiness (Phase 27): can this product safely be activated?

Pure: facts in, findings out. Every test starts from a product with everything a launch
needs and takes away one thing, so each failure names exactly the rule it broke.

The products here are synthetic. Readiness has no idea which product is which, and the
last test proves it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

import pytest

from app.core.config import Settings
from app.domain.ai_config import PlatformDefaults
from app.domain.audience import DEVELOPMENT_PLACEHOLDER_COLUMNS
from app.domain.readiness import (
    PlatformInputs,
    ProductFacts,
    ProductReadiness,
    Severity,
    Verdict,
    evaluate,
)


@dataclass
class AIRow:
    system_prompt: str | None = None
    qualification_required_fields: list[Any] | None = None
    model: str | None = None
    rag_top_k: int | None = None
    rag_min_similarity: float | None = None
    rag_max_context_chars: int | None = None
    history_turns: int | None = None
    max_tool_rounds: int | None = None
    fallback_message: str | None = None


@dataclass(frozen=True)
class SlotOverrides:
    demo_duration_minutes: int | None = None
    demo_buffer_minutes: int | None = None
    demo_working_days: list[str] | None = None
    demo_working_hours_start: str | None = None
    demo_working_hours_end: str | None = None
    demo_booking_horizon_days: int | None = None
    demo_timezone: str | None = None


CONFIRMED_SCHEMA = [
    {"source": "Full Name", "field": "name", "required": True},
    {"source": "Mobile", "field": "phone", "required": True},
]


def platform(*, production: bool = False, retrieval: bool = True) -> PlatformInputs:
    return PlatformInputs(
        ai_defaults=PlatformDefaults(
            model=None,
            rag_top_k=5,
            rag_min_similarity=0.25,
            rag_max_context_chars=4000,
            history_turns=20,
            max_tool_rounds=4,
            fallback_message="Platform fallback.",
        ),
        slot_settings=Settings(),
        retrieval_enabled=retrieval,
        production=production,
    )


def configured(**changes: Any) -> ProductFacts:
    """A product holding everything a launch needs."""
    facts = ProductFacts(
        slug="synthetic",
        display_name="Synthetic",
        is_active=False,
        audience_column_schema=CONFIRMED_SCHEMA,
        whatsapp_phone_number_id="100000000000001",
        whatsapp_business_account_id="shared-account",
        whatsapp_access_token_secret_name=None,
        google_calendar_id="synthetic@calendar.example",
        slot_overrides=SlotOverrides(),
        segment_slugs=("alpha", "beta"),
        product_ai=AIRow(
            system_prompt="The synthetic product's prompt.",
            qualification_required_fields=["interested"],
        ),
        segment_ai={},
        usable_marketing_templates=1,
        ready_documents=3,
        failed_documents=0,
    )
    return replace(facts, **changes)


def judge(facts: ProductFacts, **platform_options: bool) -> ProductReadiness:
    return evaluate(facts, platform(**platform_options))


def blockers(report: ProductReadiness) -> set[str]:
    return {finding.code for finding in report.blockers}


def gaps(report: ProductReadiness) -> set[str]:
    return {finding.code for finding in report.gaps}


class TestAFullyConfiguredProduct:
    def test_is_ready_and_stays_inactive(self) -> None:
        """READY is a verdict, not an action. Nothing here activates anything."""
        facts = configured()
        report = judge(facts)

        assert report.verdict is Verdict.READY
        assert report.findings == ()
        assert report.is_active is False
        assert facts.is_active is False
        assert report.unsafe is False

    def test_a_product_with_no_segments_is_ready(self) -> None:
        assert judge(configured(segment_slugs=())).verdict is Verdict.READY

    def test_active_and_ready_is_not_unsafe(self) -> None:
        assert judge(configured(is_active=True)).unsafe is False


class TestAIPolicy:
    def test_a_missing_policy_blocks(self) -> None:
        report = judge(configured(product_ai=None))

        assert report.verdict is Verdict.NOT_READY
        assert blockers(report) == {"ai.policy_missing"}

    def test_a_row_without_a_prompt_blocks(self) -> None:
        report = judge(configured(product_ai=AIRow(rag_top_k=6)))
        assert blockers(report) == {"ai.policy_missing"}

    def test_an_out_of_bounds_value_blocks(self) -> None:
        report = judge(configured(product_ai=AIRow(system_prompt="p", rag_top_k=0)))
        assert blockers(report) == {"ai.policy_invalid"}

    def test_an_active_product_missing_policy_is_unsafe(self) -> None:
        """SustainX active with no policy of its own: the state the readiness command
        exits non-zero for."""
        report = judge(configured(is_active=True, product_ai=None))
        assert report.unsafe is True

    def test_segments_without_overrides_share_the_product_policy(self) -> None:
        """The SustainX shape: several segments, zero override rows, and ready."""
        report = judge(configured(segment_slugs=("government", "industry"), segment_ai={}))
        assert report.verdict is Verdict.READY

    def test_sparse_segment_overrides_are_ready(self) -> None:
        """The Life Herba shape: a product default plus overrides stating only what differs."""
        report = judge(
            configured(
                segment_slugs=("pharmacies", "consumers"),
                segment_ai={
                    "pharmacies": AIRow(system_prompt="Pharmacy onboarding focus."),
                    "consumers": AIRow(qualification_required_fields=["city"]),
                },
            )
        )
        assert report.verdict is Verdict.READY

    def test_an_override_that_does_not_resolve_blocks_and_names_the_segment(self) -> None:
        report = judge(
            configured(
                segment_slugs=("pharmacies",),
                segment_ai={"pharmacies": AIRow(max_tool_rounds=0)},
            )
        )

        assert blockers(report) == {"ai.segment_override_invalid"}
        assert "'pharmacies'" in report.blockers[0].message

    def test_no_qualification_criteria_is_a_gap(self) -> None:
        report = judge(configured(product_ai=AIRow(system_prompt="p")))

        assert report.verdict is Verdict.READY_WITH_GAPS
        assert gaps(report) == {"ai.qualification_criteria_missing"}


class TestAudienceSchema:
    def test_a_missing_schema_blocks(self) -> None:
        report = judge(configured(audience_column_schema=None))
        assert blockers(report) == {"audience.schema_missing"}

    def test_a_misspelled_key_blocks(self) -> None:
        schema = [
            {"source": "Name", "field": "name", "required": True},
            {"source": "Phone", "field": "phone", "requird": True},
        ]
        report = judge(configured(audience_column_schema=schema))

        assert blockers(report) == {"audience.schema_invalid"}
        assert "requird" in report.blockers[0].message

    def test_the_placeholder_is_a_gap_outside_production(self) -> None:
        placeholder = [dict(column) for column in DEVELOPMENT_PLACEHOLDER_COLUMNS]
        assert gaps(judge(configured(audience_column_schema=placeholder))) == {
            "audience.schema_placeholder"
        }

    def test_the_placeholder_blocks_in_production(self) -> None:
        placeholder = [dict(column) for column in DEVELOPMENT_PLACEHOLDER_COLUMNS]
        report = judge(configured(audience_column_schema=placeholder), production=True)
        assert blockers(report) == {"audience.schema_placeholder"}


class TestWhatsApp:
    def test_a_missing_number_blocks(self) -> None:
        report = judge(configured(whatsapp_phone_number_id=None))
        assert blockers(report) == {"whatsapp.phone_number_missing"}

    def test_a_missing_business_account_blocks(self) -> None:
        report = judge(configured(whatsapp_business_account_id=None))
        assert blockers(report) == {"whatsapp.business_account_missing"}

    def test_a_number_held_by_another_product_blocks(self) -> None:
        report = judge(configured(phone_number_also_on=("other-product",)))

        assert blockers(report) == {"whatsapp.phone_number_shared"}
        assert "other-product" in report.blockers[0].message

    def test_one_business_account_may_serve_every_product(self) -> None:
        """The account is shared by design. Only the number must be unique."""
        first = judge(configured(slug="one", whatsapp_phone_number_id="1"))
        second = judge(configured(slug="two", whatsapp_phone_number_id="2"))
        assert first.verdict is second.verdict is Verdict.READY

    def test_a_per_product_token_override_blocks_because_nothing_reads_it(self) -> None:
        report = judge(configured(whatsapp_access_token_secret_name="own-token"))
        assert blockers(report) == {"whatsapp.token_override_unsupported"}


class TestCalendarAndSlots:
    def test_a_missing_calendar_blocks(self) -> None:
        report = judge(configured(google_calendar_id=None))
        assert blockers(report) == {"calendar.missing"}

    def test_a_calendar_held_by_another_product_blocks(self) -> None:
        report = judge(configured(calendar_also_on=("other-product",)))
        assert blockers(report) == {"calendar.shared"}

    @pytest.mark.parametrize(
        "overrides",
        [
            SlotOverrides(demo_timezone="Nowhere/Imaginary"),
            SlotOverrides(demo_working_hours_start="18:00", demo_working_hours_end="10:00"),
            SlotOverrides(demo_duration_minutes=600),
        ],
        ids=["unknown-timezone", "hours-reversed", "demo-longer-than-the-day"],
    )
    def test_an_invalid_slot_policy_blocks(self, overrides: SlotOverrides) -> None:
        report = judge(configured(slot_overrides=overrides))
        assert blockers(report) == {"slots.policy_invalid"}

    def test_a_valid_override_is_ready(self) -> None:
        report = judge(configured(slot_overrides=SlotOverrides(demo_duration_minutes=45)))
        assert report.verdict is Verdict.READY


class TestCampaignAndKnowledgeGaps:
    def test_no_usable_template_is_a_campaign_gap(self) -> None:
        report = judge(configured(usable_marketing_templates=0))

        assert report.verdict is Verdict.READY_WITH_GAPS
        assert gaps(report) == {"templates.none_usable"}
        assert all(finding.severity is Severity.GAP for finding in report.findings)

    def test_no_ready_knowledge_is_a_gap(self) -> None:
        report = judge(configured(ready_documents=0))
        assert gaps(report) == {"knowledge.none_ready"}

    def test_failed_documents_are_a_gap(self) -> None:
        report = judge(configured(failed_documents=2))
        assert gaps(report) == {"knowledge.failed_documents"}

    def test_knowledge_is_not_judged_when_retrieval_is_off(self) -> None:
        assert judge(configured(ready_documents=0), retrieval=False).verdict is Verdict.READY


class TestIdentity:
    @pytest.mark.parametrize("slug", ["Life Herba", "life_herba", "-life", "", "a" * 65])
    def test_an_invalid_slug_blocks(self, slug: str) -> None:
        assert "identity.slug_invalid" in blockers(judge(configured(slug=slug)))

    def test_a_blank_name_blocks(self) -> None:
        assert blockers(judge(configured(display_name="  "))) == {"identity.name_missing"}


class TestNothingDependsOnWhichProduct:
    def test_identical_facts_give_identical_findings_whatever_the_slug(self) -> None:
        """No `if slug == ...` anywhere: the real slugs are judged by the same rules as a
        synthetic one."""
        incomplete = {"product_ai": None, "usable_marketing_templates": 0}
        reports = [
            judge(configured(slug=slug, **incomplete))
            for slug in ("academicae", "sustainx", "life-herba", "synthetic")
        ]

        assert len({report.verdict for report in reports}) == 1
        assert len({frozenset(f.code for f in report.findings) for report in reports}) == 1
