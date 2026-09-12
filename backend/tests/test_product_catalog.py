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
    PRODUCT_FIELDS,
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
            ("government", "Government"),
            ("industry", "Industry"),
        ]
        assert [(s.slug, s.display_name) for s in products["life-herba"].segments] == [
            ("healthcare-professionals", "Healthcare Professionals"),
            ("wellness-centers", "Wellness Centers"),
            ("pharmacies", "Pharmacies"),
            ("consumers", "Consumers"),
        ]

    def test_it_invents_no_provider_ids_prompts_schemas_or_overrides(self) -> None:
        """Phone numbers, accounts, calendars, prompts, criteria and spreadsheet formats have
        not been supplied. The catalog must not contain a placeholder for any of them —
        not even a null, which would clear a value an operator had set by other means."""
        for product in load_catalog(SHIPPED_CATALOG).products:
            configured = product.model_fields_set & {
                *PRODUCT_FIELDS,
                "ai_configuration",
                "segment_ai_overrides",
            }
            assert configured == set(), product.slug

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
