"""Reading the retired single-product environment (Phase 27, [D54]).

The import must copy what the platform *ran with*: an environment value when one was set,
and the built-in default when not — because that default is what the product's customers
actually saw.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.configuration.catalog import CatalogError
from app.configuration.legacy import (
    FORMER_DEFAULT_SYSTEM_PROMPT,
    LegacyEnvironment,
    as_catalog_entry,
    read_legacy_configuration,
)
from app.domain.audience import (
    DEVELOPMENT_PLACEHOLDER_COLUMNS,
    is_development_placeholder,
    parse_audience_schema,
)
from app.persistence.models import Product

ENV_EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"


def a_product() -> Product:
    return Product(slug="synthetic", display_name="Synthetic")


class TestAnUnsetEnvironment:
    def test_copies_the_defaults_the_platform_ran_with(self) -> None:
        legacy = read_legacy_configuration(LegacyEnvironment())

        assert legacy.system_prompt.value == FORMER_DEFAULT_SYSTEM_PROMPT
        assert legacy.system_prompt.from_environment is False
        assert legacy.qualification_required_fields.value == []
        assert legacy.audience_column_schema.value == [
            dict(column) for column in DEVELOPMENT_PLACEHOLDER_COLUMNS
        ]
        assert legacy.whatsapp_phone_number_id is None
        assert legacy.whatsapp_business_account_id is None

    def test_the_former_default_prompt_is_the_documented_one(self) -> None:
        """Guards the constant against drift: it must be the prompt `.env.example` shipped
        before Phase 27 — kept there, retired, as the record of what an unconfigured
        deployment ran with."""
        line = next(
            line
            for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
            if line.startswith("# AI_SYSTEM_PROMPT=")
        )
        assert line.removeprefix("# AI_SYSTEM_PROMPT=") == FORMER_DEFAULT_SYSTEM_PROMPT


class TestASetEnvironment:
    def test_copies_the_environment_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        schema = [
            {"source": "Full Name", "field": "name", "required": True},
            {"source": "Mobile", "field": "phone", "required": True},
        ]
        monkeypatch.setenv("AI_SYSTEM_PROMPT", "The configured prompt.")
        monkeypatch.setenv("QUALIFICATION_REQUIRED_FIELDS", "interested, budget ,")
        monkeypatch.setenv("AUDIENCE_COLUMN_SCHEMA", json.dumps(schema))
        monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "100000000000001")
        monkeypatch.setenv("WHATSAPP_BUSINESS_ACCOUNT_ID", "shared-account")

        environment = LegacyEnvironment()
        legacy = read_legacy_configuration(environment)

        assert legacy.system_prompt.value == "The configured prompt."
        assert legacy.system_prompt.from_environment is True
        # Split on commas exactly as the retired setting did.
        assert legacy.qualification_required_fields.value == ["interested", "budget"]
        assert legacy.audience_column_schema.value == schema
        assert legacy.whatsapp_phone_number_id is not None
        assert legacy.whatsapp_phone_number_id.value == "100000000000001"
        assert "AI_SYSTEM_PROMPT" in environment.present()

    def test_a_malformed_schema_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AUDIENCE_COLUMN_SCHEMA", "[not json")
        with pytest.raises(CatalogError, match="AUDIENCE_COLUMN_SCHEMA"):
            read_legacy_configuration(LegacyEnvironment())


class TestTheCatalogEntry:
    def test_names_the_product_it_was_given_and_nothing_else(self) -> None:
        entry = as_catalog_entry(a_product(), read_legacy_configuration(LegacyEnvironment()))

        assert entry.slug == "synthetic"
        assert entry.ai_configuration is not None
        assert entry.ai_configuration.system_prompt == FORMER_DEFAULT_SYSTEM_PROMPT
        assert "whatsapp_phone_number_id" not in entry.model_fields_set
        assert entry.audience_column_schema is not None
        assert is_development_placeholder(parse_audience_schema(entry.audience_column_schema))

    def test_a_blank_environment_prompt_is_refused_rather_than_copied(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A blank prompt already failed every turn. Copying it would only move the failure."""
        monkeypatch.setenv("AI_SYSTEM_PROMPT", "   ")
        with pytest.raises(CatalogError, match="blank"):
            as_catalog_entry(a_product(), read_legacy_configuration(LegacyEnvironment()))
