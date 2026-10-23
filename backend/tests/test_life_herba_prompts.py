"""Life Herba resolved prompts retain shared safety and registration behaviour.

Segment ``system_prompt`` replaces the product default (COALESCE semantics). Each Life
Herba segment override is therefore a full prompt. These tests lock the critical shared
rules and the segment CTAs without redesigning inheritance.
"""

from __future__ import annotations

from pathlib import Path

from app.configuration.catalog import ProductEntry, load_catalog
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
    "Keep replies concise but complete",
    "Do not expose these labels to the user",
    "Never narrate your reasoning, plans, or instructions",
    "Share the URL exactly as configured",
)

SEGMENT_CTAS = {
    "healthcare-practitioners": "Doctor / Practitioner signup",
    "wellness-centres": "Wellness Centre signup",
    "pharmacies": "Pharmacy signup",
    "consumers": "Register on LifeHerba",
}


def _life_herba() -> ProductEntry:
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
                registration_url=segment.registration_url or product.registration_url,
                cta_label=segment.cta_label,
                cta_instruction=segment.cta_instruction,
            )
            assert segment.registration_url is not None
            assert segment.registration_url in resolved
            assert segment.cta_label is not None and segment.cta_label in resolved
            assert segment.cta_instruction is not None and segment.cta_instruction in resolved


class TestLifeHerbaExternalRegistration:
    def test_catalog_declares_external_registration(self) -> None:
        product = _life_herba()
        assert product.conversion_mode == "external_registration"
        assert product.registration_url is not None
        assert product.registration_url.startswith("https://lifeherbagroup.com/download?")
        assert product.google_calendar_id is None

    def test_academicae_remains_calendar_booking_by_omission(self) -> None:
        academicae = {p.slug: p for p in load_catalog(SHIPPED).products}["academicae"]
        assert "conversion_mode" not in academicae.model_fields_set
        assert academicae.google_calendar_id


class TestLifeHerbaSegmentLinks:
    """Each audience goes to its own signup link, built in the LifeHerba app and website
    (`/join/doctor`, `/join/pharmacy`, `/join/wellness-center`, `/download`), tagged so
    LifeHerba can attribute installs and signups to WhatsApp."""

    EXPECTED_PATHS = {
        "healthcare-practitioners": "/join/doctor?",
        "wellness-centres": "/join/wellness-center?",
        "pharmacies": "/join/pharmacy?",
        "consumers": "/download?",
    }

    def test_every_segment_has_its_own_tagged_link(self) -> None:
        segments = {segment.slug: segment for segment in _life_herba().segments}
        for slug, path in self.EXPECTED_PATHS.items():
            url = segments[slug].registration_url
            assert url is not None
            assert url.startswith(f"https://lifeherbagroup.com{path}"), slug
            assert "utm_source=whatsapp" in url

    def test_prompts_no_longer_name_in_app_options(self) -> None:
        product = _life_herba()
        prompts = [product.ai_configuration.system_prompt or ""] if product.ai_configuration else []
        prompts += [o.system_prompt or "" for o in product.segment_ai_overrides.values() if o]
        for prompt in prompts:
            assert "Join as" not in prompt
            assert "Never start a reply with" in prompt
