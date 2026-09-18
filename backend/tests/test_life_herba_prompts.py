"""Life Herba resolved prompts retain shared safety and registration behaviour.

Segment ``system_prompt`` replaces the product default (COALESCE semantics). Each Life
Herba segment override is therefore a full prompt. These tests lock the critical shared
rules and the segment CTAs without redesigning inheritance.
"""

from __future__ import annotations

from pathlib import Path

from app.configuration.catalog import load_catalog
from app.domain.conversion import with_conversion_guidance

SHIPPED = Path(__file__).resolve().parents[1] / "config" / "products.json"

SAFETY_PHRASES = (
    "NOT a doctor",
    "diagnose",
    "prescribe",
    "dosages",
    "cure a condition",
    "medical efficacy",
    "qualified healthcare professional",
    "Never borrow another product's knowledge",
    "Academicae or SustainX",
    "Do not invent prices",
    "external registration",
    "Do **not** send the registration link merely because someone replied",
    "Do not expose these labels to the user",
)

SEGMENT_CTAS = {
    "healthcare-practitioners": "Join as Doctor / Practitioner",
    "wellness-centres": "Join as Wellness Centre",
    "pharmacies": "Join as Pharmacy",
    "consumers": "Register on LifeHerba",
}


def _life_herba():
    return {p.slug: p for p in load_catalog(SHIPPED).products}["life-herba"]


class TestLifeHerbaPromptSafety:
    def test_product_default_carries_shared_safety(self) -> None:
        product = _life_herba()
        assert product.ai_configuration is not None
        prompt = product.ai_configuration.system_prompt or ""
        for phrase in SAFETY_PHRASES:
            assert phrase in prompt, phrase

    def test_every_segment_override_is_a_full_safe_prompt(self) -> None:
        product = _life_herba()
        assert set(product.segment_ai_overrides) == set(SEGMENT_CTAS)
        for slug, cta in SEGMENT_CTAS.items():
            override = product.segment_ai_overrides[slug]
            assert override is not None
            prompt = override.system_prompt or ""
            for phrase in SAFETY_PHRASES:
                assert phrase in prompt, f"{slug}: missing {phrase!r}"
            assert cta in prompt
            # Replacement semantics: segment prompt is the turn's voice, so it must not
            # be a short delta that drops the base.
            assert len(prompt) > 2000

    def test_runtime_guidance_adds_url_and_segment_cta(self) -> None:
        product = _life_herba()
        for segment in product.segments:
            override = product.segment_ai_overrides[segment.slug]
            assert override is not None and override.system_prompt
            resolved = with_conversion_guidance(
                override.system_prompt,
                mode=product.conversion_mode,
                registration_url=product.registration_url,
                cta_label=segment.cta_label,
                cta_instruction=segment.cta_instruction,
            )
            assert "https://lifeherbagroup.com/" in resolved
            assert segment.cta_label in resolved
            assert segment.cta_instruction in resolved


class TestLifeHerbaExternalRegistration:
    def test_catalog_declares_external_registration(self) -> None:
        product = _life_herba()
        assert product.conversion_mode == "external_registration"
        assert product.registration_url == "https://lifeherbagroup.com/"
        assert product.google_calendar_id is None

    def test_academicae_remains_calendar_booking_by_omission(self) -> None:
        academicae = {p.slug: p for p in load_catalog(SHIPPED).products}["academicae"]
        assert "conversion_mode" not in academicae.model_fields_set
        assert academicae.google_calendar_id
