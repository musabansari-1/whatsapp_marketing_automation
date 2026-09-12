"""Per-product audience column schemas ([D51], Phase 27).

A product's mapping is stored as JSONB, so it is validated before a spreadsheet is read with
it. And there is no mapping to fall back to: a product without one refuses uploads.
"""

from __future__ import annotations

import pytest

from app.api.v1.routes.audiences import (
    AudienceSchemaUnavailableError,
    configured_audience_schema,
)
from app.core.config import Settings
from app.domain.audience import (
    DEVELOPMENT_PLACEHOLDER_COLUMNS,
    AudienceSchemaError,
    find_missing_columns,
    is_development_placeholder,
    parse_audience_schema,
)
from app.persistence.models import Product

CONFIRMED = [
    {"source": "Full Name", "field": "name", "required": True},
    {"source": "Mobile Number", "field": "phone", "required": True},
    {"source": "Organisation", "field": "company", "required": False},
]


def a_product(schema: object) -> Product:
    return Product(slug="synthetic", display_name="Synthetic", audience_column_schema=schema)


def production() -> Settings:
    return Settings(app_env="production", app_secret="a-real-secret")  # noqa: S106


class TestParsing:
    def test_a_confirmed_mapping_parses(self) -> None:
        schema = parse_audience_schema(CONFIRMED)
        assert [column.source for column in schema.columns] == [
            "Full Name",
            "Mobile Number",
            "Organisation",
        ]

    def test_an_object_rather_than_a_list_is_refused(self) -> None:
        with pytest.raises(AudienceSchemaError, match="list"):
            parse_audience_schema({"columns": CONFIRMED})

    def test_a_misspelled_key_is_refused(self) -> None:
        """Previously ignored, which left the column silently optional."""
        with pytest.raises(AudienceSchemaError, match="requird"):
            parse_audience_schema(
                [
                    {"source": "Name", "field": "name", "required": True},
                    {"source": "Phone", "field": "phone", "required": True, "requird": True},
                ]
            )

    def test_an_unknown_canonical_field_is_refused(self) -> None:
        with pytest.raises(AudienceSchemaError, match="field"):
            parse_audience_schema(
                [
                    {"source": "Name", "field": "name", "required": True},
                    {"source": "Phone", "field": "phone", "required": True},
                    {"source": "Budget", "field": "budget", "required": False},
                ]
            )

    def test_a_mapping_without_a_required_phone_is_refused(self) -> None:
        with pytest.raises(AudienceSchemaError, match="phone"):
            parse_audience_schema([{"source": "Name", "field": "name", "required": True}])


class TestThePlaceholder:
    def test_is_recognised(self) -> None:
        schema = parse_audience_schema([dict(c) for c in DEVELOPMENT_PLACEHOLDER_COLUMNS])
        assert is_development_placeholder(schema)

    def test_a_confirmed_mapping_is_not_the_placeholder(self) -> None:
        assert not is_development_placeholder(parse_audience_schema(CONFIRMED))


class TestTwoProductsReadTheirOwnFiles:
    def test_each_mapping_expects_its_own_headers(self) -> None:
        """Product B's headers do not satisfy product A's mapping: the upload is refused as
        missing columns rather than read with the wrong fields."""
        product_a = parse_audience_schema(CONFIRMED)
        product_b = parse_audience_schema(
            [
                {"source": "Contact", "field": "name", "required": True},
                {"source": "WhatsApp", "field": "phone", "required": True},
            ]
        )

        assert find_missing_columns(["Contact", "WhatsApp"], product_a) == [
            "Full Name",
            "Mobile Number",
        ]
        assert find_missing_columns(["Contact", "WhatsApp"], product_b) == []


class TestTheUploadRefusal:
    def test_a_product_without_a_schema_is_refused(self, settings: Settings) -> None:
        with pytest.raises(AudienceSchemaUnavailableError, match="no audience column schema"):
            configured_audience_schema(a_product(None), settings)

    def test_an_invalid_stored_schema_is_refused(self, settings: Settings) -> None:
        with pytest.raises(AudienceSchemaUnavailableError, match="invalid"):
            configured_audience_schema(a_product([{"source": "Name"}]), settings)

    def test_the_placeholder_is_allowed_outside_production(self, settings: Settings) -> None:
        placeholder = [dict(c) for c in DEVELOPMENT_PLACEHOLDER_COLUMNS]
        assert configured_audience_schema(a_product(placeholder), settings)

    def test_the_placeholder_is_refused_in_production(self) -> None:
        """[D20]: production never reads a spreadsheet with the development placeholder.
        Formerly a startup guard on one environment variable; now a refusal per product."""
        placeholder = [dict(c) for c in DEVELOPMENT_PLACEHOLDER_COLUMNS]
        with pytest.raises(AudienceSchemaUnavailableError, match="placeholder"):
            configured_audience_schema(a_product(placeholder), production())

    def test_a_confirmed_schema_is_read_in_production(self) -> None:
        schema = configured_audience_schema(a_product(CONFIRMED), production())
        assert schema.source_for(schema.columns[1].field) == "Mobile Number"
