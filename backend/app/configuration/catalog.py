"""Declarative product configuration (Phase 27).

A product catalog is a JSON file naming each product and the configuration it has been
given: segments, the audience column schema, AI policy, slot policy overrides and the
non-secret provider identifiers. `python -m app.cli configure-products --file …` applies it.

Each rule below exists because its absence would hurt someone.

**Idempotent.** Applying the same catalog twice changes nothing the second time. Products and
segments are matched by slug and updated in place — never deleted and recreated — so every ID,
and every lead, audience, campaign, conversation and booking referring to one, survives.

**An absent key leaves a value alone; null clears it.** A catalog can therefore describe only
what is known. A product's entry can name its segments without inventing a calendar ID, and a
later run that adds the calendar touches nothing else.

**Activation is not configuration.** A catalog cannot set `is_active`. A product it creates
starts inactive, and an existing product keeps whatever state it had. Activation is its own
command, and it runs the readiness check first.

**Unknown keys are refused.** A misspelled `google_calender_id` would otherwise be ignored, and
the operator would believe a calendar had been configured.

**It will not make an active product less ready.** If applying a catalog would give an active
product a readiness blocker it did not already have — clearing its calendar, say — the whole
catalog is refused.

**Only non-secret values.** Phone number and business account IDs, calendar IDs, and the
*names* of secrets. Never a token.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.audience import parse_audience_schema
from app.domain.product import is_valid_slug
from app.domain.readiness import PlatformInputs
from app.persistence.models import AIConfiguration, Product, Segment
from app.persistence.repositories import ai_configurations as ai_config_repo
from app.persistence.repositories import products as product_repo
from app.persistence.repositories.readiness import evaluate_products

#: Product columns a catalog may set, named exactly as the columns are.
PRODUCT_FIELDS: tuple[str, ...] = (
    "audience_column_schema",
    "whatsapp_phone_number_id",
    "whatsapp_business_account_id",
    "google_calendar_id",
    "google_calendar_token_secret_name",
    "demo_duration_minutes",
    "demo_buffer_minutes",
    "demo_working_days",
    "demo_working_hours_start",
    "demo_working_hours_end",
    "demo_booking_horizon_days",
    "demo_timezone",
)

#: `ai_configurations` columns a catalog may set. Every one nullable: NULL inherits.
AI_FIELDS: tuple[str, ...] = (
    "system_prompt",
    "qualification_required_fields",
    "model",
    "rag_top_k",
    "rag_min_similarity",
    "rag_max_context_chars",
    "history_turns",
    "max_tool_rounds",
    "fallback_message",
)


class CatalogError(Exception):
    """A catalog cannot be applied. The caller must roll back whatever was flushed."""


# ---------------------------------------------------------------- the file's shape


def _slug(value: str) -> str:
    if not is_valid_slug(value):
        raise ValueError(f"{value!r} is not a valid slug: use lowercase words joined by hyphens")
    return value


def _text(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value


def _optional_text(value: str | None) -> str | None:
    if value is not None and not value.strip():
        raise ValueError("must not be blank; use null to clear a value")
    return value


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SegmentEntry(_Strict):
    slug: str
    display_name: str
    display_order: int | None = None
    is_active: bool | None = None

    @field_validator("slug")
    @classmethod
    def _valid_slug(cls, value: str) -> str:
        return _slug(value)

    @field_validator("display_name")
    @classmethod
    def _named(cls, value: str) -> str:
        return _text(value)


class AIConfigurationEntry(_Strict):
    """A product default or a sparse segment override. Absent fields are left alone."""

    system_prompt: str | None = None
    qualification_required_fields: list[str] | None = None
    model: str | None = None
    rag_top_k: int | None = None
    rag_min_similarity: float | None = None
    rag_max_context_chars: int | None = None
    history_turns: int | None = None
    max_tool_rounds: int | None = None
    fallback_message: str | None = None

    @field_validator("system_prompt", "model", "fallback_message")
    @classmethod
    def _not_blank(cls, value: str | None) -> str | None:
        return _optional_text(value)

    @field_validator("qualification_required_fields")
    @classmethod
    def _named_fields(cls, value: list[str] | None) -> list[str] | None:
        if value is not None:
            for item in value:
                _text(item)
        return value


class ProductEntry(_Strict):
    slug: str
    display_name: str
    #: Optional. When given it must match the existing row, or is used for a new one —
    #: which is how the original product keeps the fixed ID its migration gave it.
    id: uuid.UUID | None = None
    segments: list[SegmentEntry] = Field(default_factory=list)

    audience_column_schema: list[dict[str, Any]] | None = None
    whatsapp_phone_number_id: str | None = None
    whatsapp_business_account_id: str | None = None
    google_calendar_id: str | None = None
    google_calendar_token_secret_name: str | None = None
    demo_duration_minutes: int | None = None
    demo_buffer_minutes: int | None = None
    demo_working_days: list[str] | None = None
    demo_working_hours_start: str | None = None
    demo_working_hours_end: str | None = None
    demo_booking_horizon_days: int | None = None
    demo_timezone: str | None = None

    ai_configuration: AIConfigurationEntry | None = None
    #: Segment slug → sparse override. `null` removes that segment's override.
    segment_ai_overrides: dict[str, AIConfigurationEntry | None] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _activation_is_not_configuration(cls, data: Any) -> Any:
        if isinstance(data, dict) and "is_active" in data:
            raise ValueError(
                "is_active cannot be configured. A product is activated with "
                "`activate-product`, after its readiness check."
            )
        return data

    @field_validator("slug")
    @classmethod
    def _valid_slug(cls, value: str) -> str:
        return _slug(value)

    @field_validator("display_name")
    @classmethod
    def _named(cls, value: str) -> str:
        return _text(value)

    @field_validator(
        "whatsapp_phone_number_id",
        "whatsapp_business_account_id",
        "google_calendar_id",
        "google_calendar_token_secret_name",
        "demo_working_hours_start",
        "demo_working_hours_end",
        "demo_timezone",
    )
    @classmethod
    def _not_blank(cls, value: str | None) -> str | None:
        return _optional_text(value)

    @field_validator("audience_column_schema")
    @classmethod
    def _valid_schema(cls, value: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        if value is not None:
            parse_audience_schema(value)
        return value

    @field_validator("ai_configuration", mode="before")
    @classmethod
    def _policy_is_not_cleared_wholesale(cls, value: Any) -> Any:
        if value is None:
            raise ValueError(
                "ai_configuration cannot be null. Set individual fields to null to clear them."
            )
        return value

    @model_validator(mode="after")
    def _segments_are_distinct(self) -> ProductEntry:
        _refuse_repeats("segment slug", [segment.slug for segment in self.segments])
        for slug in self.segment_ai_overrides:
            _slug(slug)
        return self


class ProductCatalog(_Strict):
    products: list[ProductEntry]

    @model_validator(mode="after")
    def _products_are_distinct(self) -> ProductCatalog:
        _refuse_repeats("slug", [product.slug for product in self.products])
        _refuse_repeats("id", [str(product.id) for product in self.products if product.id])
        _refuse_repeats(
            "whatsapp_phone_number_id",
            [p.whatsapp_phone_number_id for p in self.products if p.whatsapp_phone_number_id],
        )
        _refuse_repeats(
            "google_calendar_id",
            [p.google_calendar_id for p in self.products if p.google_calendar_id],
        )
        return self


def _refuse_repeats(what: str, values: list[str]) -> None:
    repeated = sorted({value for value in values if values.count(value) > 1})
    if repeated:
        raise ValueError(f"{what} repeated: {', '.join(repeated)}")


def describe_validation_error(exc: ValidationError) -> str:
    lines = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"])
        lines.append(f"  {location}: {error['msg']}" if location else f"  {error['msg']}")
    return "\n".join(lines)


def load_catalog(path: Path) -> ProductCatalog:
    """Read and validate a catalog file. Nothing touches the database here."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CatalogError(f"No catalog file at {path}") from exc
    except json.JSONDecodeError as exc:
        raise CatalogError(f"{path} is not valid JSON: {exc}") from exc

    _inline_prompt_files(raw, base=path.parent)
    try:
        return ProductCatalog.model_validate(raw)
    except ValidationError as exc:
        raise CatalogError(
            f"{path} is not a valid product catalog:\n{describe_validation_error(exc)}"
        ) from exc


def _inline_prompt_files(raw: object, *, base: Path) -> None:
    """Replace `system_prompt_file` with the file's text, relative to the catalog.

    A real system prompt is pages long and reviewed as prose; keeping it in its own file
    keeps it readable and diffable. The catalog model itself only knows `system_prompt`.
    """
    if not isinstance(raw, dict):
        return
    for product in raw.get("products") or []:
        if not isinstance(product, dict):
            continue
        entries: list[object] = [product.get("ai_configuration")]
        overrides = product.get("segment_ai_overrides")
        if isinstance(overrides, dict):
            entries.extend(overrides.values())
        for entry in entries:
            if not isinstance(entry, dict) or "system_prompt_file" not in entry:
                continue
            if "system_prompt" in entry:
                raise CatalogError(
                    f"{product.get('slug')!r}: give system_prompt or system_prompt_file, not both"
                )
            relative = str(entry.pop("system_prompt_file"))
            try:
                entry["system_prompt"] = (base / relative).read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise CatalogError(f"Cannot read system_prompt_file {relative!r}: {exc}") from exc


# ---------------------------------------------------------------- applying it


async def apply_catalog(
    session: AsyncSession, catalog: ProductCatalog, *, platform: PlatformInputs
) -> list[str]:
    """Apply a catalog inside the caller's transaction, returning what changed.

    The caller commits, or rolls back for a dry run. On `CatalogError` the caller must roll
    back: work already flushed for earlier products is not undone here.
    """
    before = {
        report.slug: report.blocker_codes
        for report in await evaluate_products(session, platform)
        if report.is_active
    }

    changes: list[str] = []
    for entry in catalog.products:
        changes.extend(await apply_product(session, entry))
    await session.flush()

    worsened: list[str] = []
    for report in await evaluate_products(session, platform):
        if not report.is_active:
            continue
        new = report.blocker_codes - before.get(report.slug, frozenset())
        worsened.extend(
            f"  {report.slug}: {finding.message}"
            for finding in report.blockers
            if finding.code in new
        )
    if worsened:
        raise CatalogError(
            "This configuration would give an active product readiness blockers it does not "
            "have now:\n" + "\n".join(worsened)
        )
    return changes


async def apply_product(session: AsyncSession, entry: ProductEntry) -> list[str]:
    label = entry.slug
    changes: list[str] = []

    product = await _locate(session, entry)
    if product is None:
        product = Product(slug=entry.slug, display_name=entry.display_name, is_active=False)
        if entry.id is not None:
            product.id = entry.id
        session.add(product)
        await session.flush()
        changes.append(f"{label}: created, inactive")
    else:
        changes.extend(_set(product, "display_name", entry.display_name, label))

    for field in PRODUCT_FIELDS:
        if field not in entry.model_fields_set:
            continue
        value = getattr(entry, field)
        if field in ("whatsapp_phone_number_id", "google_calendar_id") and value:
            await _refuse_if_claimed(session, product, field, value)
        changes.extend(_set(product, field, value, label))

    changes.extend(await _apply_segments(session, product, entry.segments, label))

    if "ai_configuration" in entry.model_fields_set and entry.ai_configuration is not None:
        row = await ai_config_repo.get_product_default(session, product.id)
        if row is None:
            row = AIConfiguration(product_id=product.id, segment_id=None)
            session.add(row)
            changes.append(f"{label}: AI configuration created")
        changes.extend(_apply_ai(row, entry.ai_configuration, f"{label}: AI configuration"))

    for segment_slug, override in sorted(entry.segment_ai_overrides.items()):
        changes.extend(await _apply_override(session, product, segment_slug, override, label))

    await session.flush()
    return changes


async def _locate(session: AsyncSession, entry: ProductEntry) -> Product | None:
    by_slug = await product_repo.get_by_slug(session, entry.slug)
    if entry.id is None:
        return by_slug
    if by_slug is not None and by_slug.id != entry.id:
        raise CatalogError(
            f"Product {entry.slug!r} exists with id {by_slug.id}, not {entry.id}. The catalog "
            "will not replace a product row."
        )
    by_id = await session.get(Product, entry.id)
    if by_slug is None and by_id is not None:
        raise CatalogError(
            f"Id {entry.id} belongs to product {by_id.slug!r}, not {entry.slug!r}. Slugs are "
            "permanent route identifiers, and a catalog never renames one."
        )
    return by_slug


async def _refuse_if_claimed(
    session: AsyncSession, product: Product, field: str, value: str
) -> None:
    column = getattr(Product, field)
    result = await session.execute(
        select(Product.slug).where(column == value, Product.id != product.id)
    )
    holders = list(result.scalars().all())
    if holders:
        raise CatalogError(
            f"{product.slug}: {field} {value} is already configured on {', '.join(holders)}. "
            "Clear it there first: it can belong to one product only."
        )


async def _apply_segments(
    session: AsyncSession, product: Product, segments: list[SegmentEntry], label: str
) -> list[str]:
    """Create or update the listed segments. Segments not listed are left alone — they may
    already hold audiences and conversations, and removing one is never configuration."""
    changes: list[str] = []
    for entry in segments:
        existing = await product_repo.get_segment_by_slug(
            session, product_id=product.id, slug=entry.slug
        )
        if existing is None:
            session.add(
                Segment(
                    product_id=product.id,
                    slug=entry.slug,
                    display_name=entry.display_name,
                    display_order=entry.display_order or 0,
                    is_active=True if entry.is_active is None else entry.is_active,
                )
            )
            changes.append(f"{label}: segment {entry.slug} created")
            continue
        segment_label = f"{label}: segment {entry.slug}"
        changes.extend(_set(existing, "display_name", entry.display_name, segment_label))
        if entry.display_order is not None:
            changes.extend(_set(existing, "display_order", entry.display_order, segment_label))
        if entry.is_active is not None:
            changes.extend(_set(existing, "is_active", entry.is_active, segment_label))
    await session.flush()
    return changes


async def _apply_override(
    session: AsyncSession,
    product: Product,
    segment_slug: str,
    override: AIConfigurationEntry | None,
    label: str,
) -> list[str]:
    segment = await product_repo.get_segment_by_slug(
        session, product_id=product.id, slug=segment_slug
    )
    if segment is None:
        raise CatalogError(
            f"{label}: segment_ai_overrides names segment {segment_slug!r}, which this product "
            "does not have"
        )
    row = await ai_config_repo.get_segment_override(session, product.id, segment.id)
    override_label = f"{label}: AI override for {segment_slug}"

    if override is None:
        if row is None:
            return []
        await session.delete(row)
        return [f"{override_label} removed"]

    changes: list[str] = []
    if row is None:
        row = AIConfiguration(product_id=product.id, segment_id=segment.id)
        session.add(row)
        changes.append(f"{override_label} created")
    changes.extend(_apply_ai(row, override, override_label))
    return changes


def _apply_ai(row: AIConfiguration, entry: AIConfigurationEntry, label: str) -> list[str]:
    changes: list[str] = []
    for field in AI_FIELDS:
        if field in entry.model_fields_set:
            changes.extend(_set(row, field, getattr(entry, field), label))
    return changes


def _set(target: object, field: str, value: object, label: str) -> list[str]:
    current = getattr(target, field)
    if current == value:
        return []
    setattr(target, field, value)
    if value is None:
        verb = "cleared"
    elif current is None:
        verb = "set"
    else:
        verb = "updated"
    return [f"{label}: {field} {verb}"]


__all__ = [
    "AI_FIELDS",
    "PRODUCT_FIELDS",
    "AIConfigurationEntry",
    "CatalogError",
    "ProductCatalog",
    "ProductEntry",
    "SegmentEntry",
    "apply_catalog",
    "apply_product",
    "describe_validation_error",
    "load_catalog",
]
