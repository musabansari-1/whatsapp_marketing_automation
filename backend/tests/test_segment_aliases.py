"""Obsolete → canonical segment alias maps for Life Herba and SustainX."""

from __future__ import annotations

from pathlib import Path

from app.configuration.catalog import load_catalog
from app.configuration.segment_aliases import (
    LIFE_HERBA_SEGMENT_RENAMES,
    LIFE_HERBA_SLUG,
    SUSTAINX_AMBIGUOUS_OBSOLETE_SLUGS,
    SUSTAINX_CANONICAL_SLUGS,
    SUSTAINX_SEGMENT_RENAMES,
    SUSTAINX_SLUG,
)

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


class TestSustainXSegmentAliases:
    def test_clear_renames_only(self) -> None:
        assert SUSTAINX_SEGMENT_RENAMES == {
            "government": ("government-bodies", "Government Bodies"),
            "industry": ("industries", "Industries"),
            "universities": ("institutions", "Institutions"),
        }

    def test_ambiguous_experimental_slugs_are_never_auto_mapped(self) -> None:
        for slug in SUSTAINX_AMBIGUOUS_OBSOLETE_SLUGS:
            assert slug not in SUSTAINX_SEGMENT_RENAMES
            assert slug not in SUSTAINX_CANONICAL_SLUGS

    def test_shipped_catalog_lists_only_canonical_segments(self) -> None:
        sustainx = {p.slug: p for p in load_catalog(SHIPPED).products}[SUSTAINX_SLUG]
        slugs = [s.slug for s in sustainx.segments]
        assert slugs == [
            "industries",
            "institutions",
            "government-bodies",
            "students",
            "professionals-executives",
        ]
        assert set(slugs) == SUSTAINX_CANONICAL_SLUGS
        for obsolete in SUSTAINX_SEGMENT_RENAMES:
            assert obsolete not in slugs
        for obsolete in SUSTAINX_AMBIGUOUS_OBSOLETE_SLUGS:
            assert obsolete not in slugs

    def test_planned_template_names_are_locked_in_docs(self) -> None:
        readme = (Path(__file__).resolve().parents[1] / "config" / "README.md").read_text(
            encoding="utf-8"
        )
        for name in (
            "sustainx_industries_outreach",
            "sustainx_institutions_outreach",
            "sustainx_government_bodies_outreach",
            "sustainx_students_outreach",
            "sustainx_professionals_executives_outreach",
        ):
            assert name in readme
        # Obsolete names may appear only as explicitly retired / do-not-use.
        assert "Retired obsolete names" in readme
        retired_section = readme.split("Retired obsolete names", 1)[1].split("##", 1)[0]
        assert "sustainx_government_outreach" in retired_section
        assert "sustainx_industry_outreach" in retired_section
        # Active planned table must use government_bodies, not the old government name.
        assert "| Government Bodies | `sustainx_government_bodies_outreach` |" in readme
