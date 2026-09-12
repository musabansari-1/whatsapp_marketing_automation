"""Copy the pre-Phase-27 single-product configuration into one product ([D54], Phase 27).

Before Phase 27 the original product's policy and spreadsheet format lived in the platform
environment — `AI_SYSTEM_PROMPT`, `QUALIFICATION_REQUIRED_FIELDS`, `AUDIENCE_COLUMN_SCHEMA` —
along with the WhatsApp number in `WHATSAPP_PHONE_NUMBER_ID` and
`WHATSAPP_BUSINESS_ACCOUNT_ID`. The platform no longer reads any of them at runtime. This
moves them, once, into the product an operator names.

**It copies what the platform actually ran with.** Where a variable was never set the
platform used a built-in default, and that default is what the product's customers saw, so
the default is what gets copied — labelled as such. Anything else would change the product's
behaviour in the act of preserving it.

**The product is named explicitly.** The command takes a slug. Nothing here decides that the
environment belongs to the first product, the only product, or one with a particular name.

**It never overwrites silently.** A value the product already holds that differs from the
environment is a conflict, and nothing is written unless the operator passes `--overwrite`.
Re-running with the same environment changes nothing.

Not a migration, because a migration must not read deployment settings, and not automatic,
because which product the environment described is a fact only an operator holds.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.configuration.catalog import (
    CatalogError,
    ProductCatalog,
    ProductEntry,
    apply_catalog,
    describe_validation_error,
)
from app.domain.audience import DEVELOPMENT_PLACEHOLDER_COLUMNS
from app.domain.readiness import PlatformInputs
from app.persistence.models import Product
from app.persistence.repositories import ai_configurations as ai_config_repo

#: The system prompt the platform used when `AI_SYSTEM_PROMPT` was not set, exactly as it
#: stood in `Settings` before Phase 27. Kept only so an import is faithful; nothing reads it
#: at runtime.
FORMER_DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful assistant replying to a customer on WhatsApp on behalf of "
    "the business. Be brief and professional. Only state facts that appear in the "
    "conversation or in verified information provided to you. If you do not know "
    "something, say you will have a colleague follow up. Never invent prices, "
    "availability, or commitments."
)


class LegacyEnvironment(BaseSettings):
    """The retired single-product variables, read only by the import.

    Raw strings, parsed below, so a malformed value produces an import error rather than a
    startup failure somewhere else.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    ai_system_prompt: str | None = None
    qualification_required_fields: str | None = None
    audience_column_schema: str | None = None
    whatsapp_phone_number_id: str | None = None
    whatsapp_business_account_id: str | None = None

    def present(self) -> list[str]:
        """The variables that are set, by environment name."""
        return [name.upper() for name, value in self if value is not None]


@dataclass(frozen=True, slots=True)
class LegacyValue:
    variable: str
    value: object
    from_environment: bool


@dataclass(frozen=True, slots=True)
class LegacyConfiguration:
    system_prompt: LegacyValue
    qualification_required_fields: LegacyValue
    audience_column_schema: LegacyValue
    whatsapp_phone_number_id: LegacyValue | None
    whatsapp_business_account_id: LegacyValue | None


def read_legacy_configuration(environment: LegacyEnvironment) -> LegacyConfiguration:
    """What the platform effectively ran with, variable by variable."""
    prompt = environment.ai_system_prompt
    criteria = environment.qualification_required_fields
    schema = environment.audience_column_schema

    if schema is not None:
        try:
            schema_value: object = json.loads(schema)
        except json.JSONDecodeError as exc:
            raise CatalogError(f"AUDIENCE_COLUMN_SCHEMA is not valid JSON: {exc}") from exc
    else:
        schema_value = [dict(column) for column in DEVELOPMENT_PLACEHOLDER_COLUMNS]

    def identifier(variable: str, value: str | None) -> LegacyValue | None:
        return LegacyValue(variable, value, True) if value else None

    return LegacyConfiguration(
        system_prompt=LegacyValue(
            "AI_SYSTEM_PROMPT",
            prompt if prompt is not None else FORMER_DEFAULT_SYSTEM_PROMPT,
            prompt is not None,
        ),
        # The old setting split on commas, and unset meant an empty list: no criteria.
        qualification_required_fields=LegacyValue(
            "QUALIFICATION_REQUIRED_FIELDS",
            [item.strip() for item in (criteria or "").split(",") if item.strip()],
            criteria is not None,
        ),
        audience_column_schema=LegacyValue(
            "AUDIENCE_COLUMN_SCHEMA", schema_value, schema is not None
        ),
        whatsapp_phone_number_id=identifier(
            "WHATSAPP_PHONE_NUMBER_ID", environment.whatsapp_phone_number_id
        ),
        whatsapp_business_account_id=identifier(
            "WHATSAPP_BUSINESS_ACCOUNT_ID", environment.whatsapp_business_account_id
        ),
    )


def describe_sources(legacy: LegacyConfiguration) -> list[str]:
    """One line per variable, saying where the value that will be copied came from."""
    lines = []
    for value in (
        legacy.system_prompt,
        legacy.qualification_required_fields,
        legacy.audience_column_schema,
    ):
        source = (
            "from the environment"
            if value.from_environment
            else "not set: the built-in default the platform ran with"
        )
        lines.append(f"  {value.variable}: {source}")
    for name, identifier in (
        ("WHATSAPP_PHONE_NUMBER_ID", legacy.whatsapp_phone_number_id),
        ("WHATSAPP_BUSINESS_ACCOUNT_ID", legacy.whatsapp_business_account_id),
    ):
        lines.append(
            f"  {name}: from the environment" if identifier else f"  {name}: not set, left alone"
        )
    return lines


def as_catalog_entry(product: Product, legacy: LegacyConfiguration) -> ProductEntry:
    data: dict[str, object] = {
        "slug": product.slug,
        "display_name": product.display_name,
        "audience_column_schema": legacy.audience_column_schema.value,
        "ai_configuration": {
            "system_prompt": legacy.system_prompt.value,
            "qualification_required_fields": legacy.qualification_required_fields.value,
        },
    }
    if legacy.whatsapp_phone_number_id is not None:
        data["whatsapp_phone_number_id"] = legacy.whatsapp_phone_number_id.value
    if legacy.whatsapp_business_account_id is not None:
        data["whatsapp_business_account_id"] = legacy.whatsapp_business_account_id.value
    try:
        return ProductEntry.model_validate(data)
    except ValidationError as exc:
        raise CatalogError(
            "The legacy environment cannot be imported as it stands:\n"
            + describe_validation_error(exc)
        ) from exc


class LegacyImportConflictError(CatalogError):
    """The product already holds different values. Nothing was written."""


async def import_legacy_configuration(
    session: AsyncSession,
    product: Product,
    legacy: LegacyConfiguration,
    *,
    overwrite: bool,
    platform: PlatformInputs,
) -> list[str]:
    """Copy the legacy values into `product`, inside the caller's transaction."""
    entry = as_catalog_entry(product, legacy)
    if not overwrite:
        conflicts = await _conflicts(session, product, entry)
        if conflicts:
            raise LegacyImportConflictError(
                f"{product.slug} already holds different values for: {', '.join(conflicts)}. "
                "Nothing was written. Re-run with --overwrite to replace them."
            )
    return await apply_catalog(session, ProductCatalog(products=[entry]), platform=platform)


async def _conflicts(session: AsyncSession, product: Product, entry: ProductEntry) -> list[str]:
    conflicts: list[str] = []
    for field in (
        "audience_column_schema",
        "whatsapp_phone_number_id",
        "whatsapp_business_account_id",
    ):
        if field not in entry.model_fields_set:
            continue
        current = getattr(product, field)
        if current is not None and current != getattr(entry, field):
            conflicts.append(field)

    row = await ai_config_repo.get_product_default(session, product.id)
    if row is not None and entry.ai_configuration is not None:
        for field in ("system_prompt", "qualification_required_fields"):
            current = getattr(row, field)
            if current is not None and current != getattr(entry.ai_configuration, field):
                conflicts.append(field)
    return conflicts


__all__ = [
    "FORMER_DEFAULT_SYSTEM_PROMPT",
    "LegacyConfiguration",
    "LegacyEnvironment",
    "LegacyImportConflictError",
    "LegacyValue",
    "as_catalog_entry",
    "describe_sources",
    "import_legacy_configuration",
    "read_legacy_configuration",
]
