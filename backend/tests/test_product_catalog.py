"""The product catalog's shape (Phase 27).

Validation only — applying a catalog is tested against PostgreSQL. What matters here is
that a mistake in the file is refused with a reason before anything touches a database, and
that the catalog shipped in the repository seeds only what the product owner confirmed.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.configuration.catalog import (
    CatalogError,
    ProductCatalog,
    ProductEntry,
    load_catalog,
)

SHIPPED_CATALOG = Path(__file__).resolve().parents[1] / "config" / "products.json"
ACADEMICAE_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")


def entry(**fields: Any) -> dict[str, Any]:
    return {"slug": "synthetic", "display_name": "Synthetic", **fields}


class TestTheShippedCatalog:
    def test_it_is_valid(self) -> None:
        load_catalog(SHIPPED_CATALOG)

    def test_it_names_the_three_confirmed_products(self) -> None:
        catalog = load_catalog(SHIPPED_CATALOG)
        assert [(p.slug, p.display_name) for p in catalog.products] == [
            ("academicae", "Academicae"),
            ("sustainx", "SustainX"),
            ("life-herba", "Life Herba"),
        ]

    def test_academicae_keeps_its_fixed_id_and_has_no_invented_segments(self) -> None:
        academicae = load_catalog(SHIPPED_CATALOG).products[0]
        assert academicae.id == ACADEMICAE_ID
        assert academicae.segments == []

    def test_the_confirmed_segments_are_seeded_in_order(self) -> None:
        products = {p.slug: p for p in load_catalog(SHIPPED_CATALOG).products}
        assert [(s.slug, s.display_name) for s in products["sustainx"].segments] == [
            ("industries", "Industries"),
            ("institutions", "Institutions"),
            ("government-bodies", "Government Bodies"),
            ("students", "Students"),
            ("professionals-executives", "Professionals / Executives"),
        ]
        assert [(s.slug, s.display_name) for s in products["life-herba"].segments] == [
            ("healthcare-practitioners", "Healthcare Practitioners"),
            ("wellness-centres", "Wellness Centres"),
            ("pharmacies", "Pharmacies"),
            ("consumers", "Consumers"),
        ]

    def test_sustainx_has_confirmed_launch_configuration(self) -> None:
        """SustainX launch config includes its own WhatsApp number and WABA."""
        products = {p.slug: p for p in load_catalog(SHIPPED_CATALOG).products}
        product = products["sustainx"]
        academicae = products["academicae"]
        life_herba = products["life-herba"]

        assert product.google_calendar_id == (
            "15ab1950b41e20b63b6bd2ef1aad5506808ac502c9722c11267733d882e101b1@"
            "group.calendar.google.com"
        )
        assert product.whatsapp_phone_number_id == "1355444384311332"
        assert product.whatsapp_business_account_id == "1107203798409996"
        # Routing uses phone_number_id ([D40]); SustainX must not reuse another product.
        assert product.whatsapp_phone_number_id != academicae.whatsapp_phone_number_id
        assert product.whatsapp_phone_number_id != life_herba.whatsapp_phone_number_id
        assert product.whatsapp_business_account_id != academicae.whatsapp_business_account_id
        assert product.whatsapp_business_account_id != life_herba.whatsapp_business_account_id
        assert product.ai_configuration is not None
        assert product.ai_configuration.qualification_mode == "ai_judgement"
        assert product.ai_configuration.system_prompt
        assert "presume" in product.ai_configuration.system_prompt.lower() or (
            "does NOT tell you what" in product.ai_configuration.system_prompt
        )
        assert product.segment_ai_overrides == {}
        assert product.demo_duration_minutes == 60
        assert product.demo_buffer_minutes == 15
        assert product.demo_booking_horizon_days == -1
        assert product.demo_min_lead_days == 1
        assert product.demo_timezone == "Asia/Kolkata"
        assert product.audience_column_schema == [
            {"source": "Contact Name", "field": "name", "required": True},
            {"source": "Phone", "field": "phone", "required": True},
            {"source": "Email", "field": "email", "required": False},
            {"source": "Organization Name", "field": "company", "required": False},
            {"source": "Designation", "field": "designation", "required": False},
            {"source": "City", "field": "city", "required": False},
        ]

    def test_life_herba_has_registration_launch_configuration(self) -> None:
        """Life Herba launch config: AI, conversion, audience schema, own WhatsApp number."""
        products = {p.slug: p for p in load_catalog(SHIPPED_CATALOG).products}
        product = products["life-herba"]
        academicae = products["academicae"]
        assert product.conversion_mode == "external_registration"
        assert product.registration_url == "https://lifeherbagroup.com/"
        assert product.ai_configuration is not None
        assert product.ai_configuration.qualification_mode == "ai_judgement"
        assert set(product.segment_ai_overrides) == {
            "healthcare-practitioners",
            "wellness-centres",
            "pharmacies",
            "consumers",
        }
        assert product.whatsapp_phone_number_id == "1340886312439722"
        assert product.whatsapp_business_account_id == "2034571340595521"
        # Routing uses the unique phone number ID ([D40]); Life Herba has its own WABA.
        assert product.whatsapp_phone_number_id != academicae.whatsapp_phone_number_id
        assert product.whatsapp_business_account_id != academicae.whatsapp_business_account_id
        assert product.google_calendar_id is None
        assert product.audience_column_schema == [
            {"source": "Name", "field": "name", "required": True},
            {"source": "Phone", "field": "phone", "required": True},
            {"source": "Email", "field": "email", "required": False},
            {"source": "City", "field": "city", "required": False},
        ]
        for segment in product.segments:
            assert segment.cta_label
            assert segment.cta_instruction

    def test_academicae_has_confirmed_launch_configuration(self) -> None:
        academicae = {p.slug: p for p in load_catalog(SHIPPED_CATALOG).products}["academicae"]

        assert academicae.whatsapp_phone_number_id
        assert academicae.google_calendar_id
        assert academicae.audience_column_schema
        assert academicae.ai_configuration is not None
        assert academicae.ai_configuration.qualification_mode == "ai_judgement"
        assert academicae.demo_booking_horizon_days == -1
        assert academicae.demo_min_lead_days == 1
        assert academicae.demo_duration_minutes == 60

    def test_it_cannot_activate_anything(self) -> None:
        raw = json.loads(SHIPPED_CATALOG.read_text(encoding="utf-8"))
        assert all("is_active" not in product for product in raw["products"])


class TestMistakesAreRefused:
    def test_activation_is_not_configuration(self) -> None:
        with pytest.raises(ValidationError, match="activate-product"):
            ProductEntry.model_validate(entry(is_active=True))

    def test_an_unknown_key_is_refused_not_ignored(self) -> None:
        with pytest.raises(ValidationError, match="google_calender_id"):
            ProductEntry.model_validate(entry(google_calender_id="c@example.com"))

    def test_a_misspelled_audience_key_is_refused(self) -> None:
        schema = [
            {"source": "Name", "field": "name", "required": True},
            {"source": "Mobile", "field": "phone", "requird": True},
        ]
        with pytest.raises(ValidationError, match="requird"):
            ProductEntry.model_validate(entry(audience_column_schema=schema))

    def test_a_schema_without_a_phone_column_is_refused(self) -> None:
        schema = [{"source": "Name", "field": "name", "required": True}]
        with pytest.raises(ValidationError, match="phone column"):
            ProductEntry.model_validate(entry(audience_column_schema=schema))

    @pytest.mark.parametrize("slug", ["Life Herba", "life_herba", "SUSTAINX"])
    def test_an_invalid_slug_is_refused(self, slug: str) -> None:
        with pytest.raises(ValidationError, match="slug"):
            ProductEntry.model_validate(entry(slug=slug))

    def test_a_repeated_segment_is_refused(self) -> None:
        segments = [
            {"slug": "government", "display_name": "Government"},
            {"slug": "government", "display_name": "Government again"},
        ]
        with pytest.raises(ValidationError, match="segment slug repeated"):
            ProductEntry.model_validate(entry(segments=segments))

    def test_two_products_cannot_share_a_number_or_a_calendar(self) -> None:
        with pytest.raises(ValidationError, match="whatsapp_phone_number_id repeated"):
            ProductCatalog.model_validate(
                {
                    "products": [
                        entry(slug="one", whatsapp_phone_number_id="1"),
                        entry(slug="two", whatsapp_phone_number_id="1"),
                    ]
                }
            )
        with pytest.raises(ValidationError, match="google_calendar_id repeated"):
            ProductCatalog.model_validate(
                {
                    "products": [
                        entry(slug="one", google_calendar_id="c"),
                        entry(slug="two", google_calendar_id="c"),
                    ]
                }
            )

    def test_two_entries_for_one_product_are_refused(self) -> None:
        with pytest.raises(ValidationError, match="slug repeated"):
            ProductCatalog.model_validate({"products": [entry(), entry()]})

    def test_one_business_account_may_serve_several_products(self) -> None:
        ProductCatalog.model_validate(
            {
                "products": [
                    entry(slug="one", whatsapp_business_account_id="shared"),
                    entry(slug="two", whatsapp_business_account_id="shared"),
                ]
            }
        )

    def test_a_policy_cannot_be_cleared_wholesale(self) -> None:
        with pytest.raises(ValidationError, match="ai_configuration cannot be null"):
            ProductEntry.model_validate(entry(ai_configuration=None))

    def test_a_blank_prompt_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="blank"):
            ProductEntry.model_validate(entry(ai_configuration={"system_prompt": "  "}))

    def test_unbounded_horizon_is_stored_as_minus_one(self) -> None:
        product = ProductEntry.model_validate(entry(demo_booking_horizon_days="unbounded"))
        assert product.demo_booking_horizon_days == -1

    def test_qualification_mode_ai_judgement_is_accepted(self) -> None:
        product = ProductEntry.model_validate(
            entry(ai_configuration={"qualification_mode": "ai_judgement"})
        )
        assert product.ai_configuration is not None
        assert product.ai_configuration.qualification_mode == "ai_judgement"


class TestMeetingUrl:
    """The product's permanent meeting link (Option 2, [D65])."""

    def test_an_https_meeting_url_is_accepted(self) -> None:
        product = ProductEntry.model_validate(
            entry(meeting_url="https://meet.google.com/abc-defg-hij")
        )
        assert product.meeting_url == "https://meet.google.com/abc-defg-hij"

    def test_a_non_https_meeting_url_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="https://"):
            ProductEntry.model_validate(entry(meeting_url="meet.google.com/abc-defg-hij"))

    def test_a_blank_meeting_url_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="blank"):
            ProductEntry.model_validate(entry(meeting_url="  "))

    def test_a_null_meeting_url_clears(self) -> None:
        clearing = ProductEntry.model_validate(entry(meeting_url=None))
        assert "meeting_url" in clearing.model_fields_set

    def test_two_products_may_not_share_a_meeting_url(self) -> None:
        with pytest.raises(ValidationError, match="meeting_url repeated"):
            ProductCatalog.model_validate(
                {
                    "products": [
                        entry(slug="one", meeting_url="https://meet.google.com/abc-defg-hij"),
                        entry(slug="two", meeting_url="https://meet.google.com/abc-defg-hij"),
                    ]
                }
            )


class TestAbsentIsNotNull:
    def test_an_absent_key_is_left_alone_and_a_null_clears(self) -> None:
        """The distinction that lets a catalog describe only what is known."""
        silent = ProductEntry.model_validate(entry())
        clearing = ProductEntry.model_validate(entry(google_calendar_id=None))

        assert "google_calendar_id" not in silent.model_fields_set
        assert "google_calendar_id" in clearing.model_fields_set


class TestPromptFiles:
    def _write(self, directory: Path, product: dict[str, Any]) -> Path:
        path = directory / "catalog.json"
        path.write_text(json.dumps({"products": [product]}), encoding="utf-8")
        return path

    def test_a_prompt_file_is_read_relative_to_the_catalog(self, tmp_path: Path) -> None:
        (tmp_path / "prompts").mkdir()
        (tmp_path / "prompts" / "synthetic.md").write_text("A long prompt.\n", encoding="utf-8")
        path = self._write(
            tmp_path,
            entry(
                ai_configuration={"system_prompt_file": "prompts/synthetic.md"},
                segments=[{"slug": "alpha", "display_name": "Alpha"}],
                segment_ai_overrides={"alpha": {"system_prompt_file": "prompts/synthetic.md"}},
            ),
        )

        product = load_catalog(path).products[0]

        assert product.ai_configuration is not None
        assert product.ai_configuration.system_prompt == "A long prompt."
        override = product.segment_ai_overrides["alpha"]
        assert override is not None
        assert override.system_prompt == "A long prompt."

    def test_a_missing_prompt_file_is_refused(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, entry(ai_configuration={"system_prompt_file": "gone.md"}))
        with pytest.raises(CatalogError, match="gone.md"):
            load_catalog(path)

    def test_a_prompt_and_a_prompt_file_together_are_refused(self, tmp_path: Path) -> None:
        (tmp_path / "p.md").write_text("x", encoding="utf-8")
        path = self._write(
            tmp_path,
            entry(ai_configuration={"system_prompt": "inline", "system_prompt_file": "p.md"}),
        )
        with pytest.raises(CatalogError, match="not both"):
            load_catalog(path)

    def test_malformed_json_is_refused_with_its_location(self, tmp_path: Path) -> None:
        path = tmp_path / "broken.json"
        path.write_text("{ not json", encoding="utf-8")
        with pytest.raises(CatalogError, match="not valid JSON"):
            load_catalog(path)
