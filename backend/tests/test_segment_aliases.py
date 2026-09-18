"""Life Herba obsolete → canonical segment alias map."""

from __future__ import annotations

from pathlib import Path

from app.configuration.catalog import load_catalog
from app.configuration.segment_aliases import LIFE_HERBA_SEGMENT_RENAMES, LIFE_HERBA_SLUG

SHIPPED = Path(__file__).resolve().parents[1] / "config" / "products.json"


class TestLifeHerbaSegmentAliases:
    def test_obsolete_slugs_map_to_canonical_launch_set(self) -> None:
        assert LIFE_HERBA_SEGMENT_RENAMES == {
            "healthcare-professionals": (
                "healthcare-practitioners",
                "Healthcare Practitioners",
            ),
            "wellness-centers": ("wellness-centres", "Wellness Centres"),
        }

    def test_shipped_catalog_lists_only_canonical_segments(self) -> None:
        life_herba = {p.slug: p for p in load_catalog(SHIPPED).products}[LIFE_HERBA_SLUG]
        slugs = [s.slug for s in life_herba.segments]
        assert slugs == [
            "healthcare-practitioners",
            "wellness-centres",
            "pharmacies",
            "consumers",
        ]
        for obsolete in LIFE_HERBA_SEGMENT_RENAMES:
            assert obsolete not in slugs

    def test_planned_template_names_are_locked_in_docs(self) -> None:
        readme = (Path(__file__).resolve().parents[1] / "config" / "README.md").read_text(
            encoding="utf-8"
        )
        for name in (
            "lifeherba_healthcare_practitioner_onboarding",
            "lifeherba_wellness_centre_onboarding",
            "lifeherba_pharmacy_onboarding",
            "lifeherba_consumer_outreach",
        ):
            assert name in readme
        assert "lifeherba_practitioner_onboarding" not in readme
