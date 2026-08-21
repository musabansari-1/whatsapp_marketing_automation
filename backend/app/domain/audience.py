"""Audience column schema and row validation (specification §6, [D4], [D20]).

The client's production column names are deliberately absent. [D20] forbids inventing
them, so the mapping between spreadsheet headers and platform fields is *data*: a
configured list of column mappings the pipeline is pointed at. When the client
confirms their format it is set in configuration, with no code change.

Everything here is pure. Parsing bytes is `app.domain.excel`; persistence is the
repository. That keeps the validation rules testable against plain dictionaries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator, model_validator

from app.domain.enums import ContactValidationStatus
from app.domain.phone import normalize_phone


class AudienceField(StrEnum):
    """The canonical V1 contact fields ([D25]).

    Stable regardless of what the client names their spreadsheet columns; the mapping
    between the two is configuration. Columns mapping to nothing are still preserved
    in the row's `raw_row`, so no uploaded data is silently discarded.
    """

    NAME = "name"
    PHONE = "phone"
    EMAIL = "email"
    COMPANY = "company"
    DESIGNATION = "designation"
    CITY = "city"


# [D25]: both must be present and populated for a row to be usable.
REQUIRED_FIELDS = frozenset({AudienceField.NAME, AudienceField.PHONE})


class ColumnMapping(BaseModel):
    """One spreadsheet column, mapped to a platform field.

    `source` is the header text as it appears in the client's file. It is matched
    case-insensitively and ignoring surrounding whitespace, because spreadsheet
    headers are typed by humans.
    """

    source: str = Field(min_length=1)
    field: AudienceField
    required: bool = False

    @field_validator("source")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()


class AudienceColumnSchema(BaseModel):
    """The configured mapping for the client's Excel format.

    Name and phone must both be mapped and marked required ([D25]). The phone is the
    platform's WhatsApp identity ([D11]); the name is what every downstream surface
    displays, and a contact the salesperson cannot identify is not usable.
    """

    columns: list[ColumnMapping]

    @model_validator(mode="after")
    def _validate(self) -> AudienceColumnSchema:
        if not self.columns:
            raise ValueError("audience column schema must define at least one column")

        fields = [column.field for column in self.columns]
        duplicates = {f for f in fields if fields.count(f) > 1}
        if duplicates:
            raise ValueError(f"duplicate field mappings: {sorted(f.value for f in duplicates)}")

        sources = [column.source.casefold() for column in self.columns]
        duplicate_sources = {s for s in sources if sources.count(s) > 1}
        if duplicate_sources:
            raise ValueError(f"duplicate source columns: {sorted(duplicate_sources)}")

        mapped = {column.field: column for column in self.columns}
        for required in sorted(REQUIRED_FIELDS, key=lambda f: f.value):
            column = mapped.get(required)
            if column is None:
                raise ValueError(f"audience column schema must map a {required.value} column")
            if not column.required:
                raise ValueError(f"the {required.value} column must be required")

        return self

    @property
    def required_sources(self) -> list[str]:
        return [column.source for column in self.columns if column.required]

    def source_for(self, target: AudienceField) -> str | None:
        return next((c.source for c in self.columns if c.field is target), None)


class MissingColumnsError(Exception):
    """The uploaded file lacks a required column (§6).

    Raised for the file as a whole rather than per row: a missing required column
    means the wrong file, and processing it row by row would produce thousands of
    identical errors.
    """

    def __init__(self, missing: list[str]) -> None:
        super().__init__("Missing required column(s): " + ", ".join(missing))
        self.missing = missing


@dataclass(frozen=True, slots=True)
class ValidatedRow:
    """One spreadsheet row after validation."""

    row_number: int
    raw_row: dict[str, object]
    status: ContactValidationStatus
    error: str | None = None
    phone_e164: str | None = None
    full_name: str | None = None
    email: str | None = None
    company: str | None = None
    designation: str | None = None
    city: str | None = None


@dataclass(slots=True)
class ValidationSummary:
    """Counts and rows for the §6 preview."""

    total: int = 0
    valid: int = 0
    invalid: int = 0
    duplicate: int = 0
    rows: list[ValidatedRow] = field(default_factory=list)


def find_missing_columns(headers: list[str], schema: AudienceColumnSchema) -> list[str]:
    """Required columns absent from the file, compared case-insensitively."""
    present = {header.strip().casefold() for header in headers}
    return [source for source in schema.required_sources if source.casefold() not in present]


def validate_rows(
    rows: list[dict[str, object]],
    *,
    schema: AudienceColumnSchema,
    default_region: str,
) -> ValidationSummary:
    """Validate parsed rows, marking each VALID, INVALID or DUPLICATE.

    Invalid and duplicate rows are kept rather than dropped: §6 requires the preview
    to show what was rejected and why, which is also why the schema stores them.

    Duplicates are decided on the *normalized* number, so two spellings of one number
    are caught. The first occurrence wins and later ones are DUPLICATE, making the
    outcome depend on file order rather than on processing order.
    """
    summary = ValidationSummary()
    seen_phones: set[str] = set()

    for index, raw_row in enumerate(rows, start=1):
        summary.total += 1
        validated = _validate_row(
            index, raw_row, schema=schema, default_region=default_region, seen=seen_phones
        )

        if validated.status is ContactValidationStatus.VALID and validated.phone_e164:
            seen_phones.add(validated.phone_e164)

        match validated.status:
            case ContactValidationStatus.VALID:
                summary.valid += 1
            case ContactValidationStatus.DUPLICATE:
                summary.duplicate += 1
            case ContactValidationStatus.INVALID:
                summary.invalid += 1

        summary.rows.append(validated)

    return summary


def _validate_row(
    row_number: int,
    raw_row: dict[str, object],
    *,
    schema: AudienceColumnSchema,
    default_region: str,
    seen: set[str],
) -> ValidatedRow:
    phone = normalize_phone(
        _lookup(raw_row, schema.source_for(AudienceField.PHONE)),
        default_region=default_region,
    )
    if not phone.ok:
        return ValidatedRow(row_number, raw_row, ContactValidationStatus.INVALID, phone.error)

    # [D25] makes name required, so a row with a usable phone but no name is INVALID.
    full_name = _text(_lookup(raw_row, schema.source_for(AudienceField.NAME))) or None
    if full_name is None:
        return ValidatedRow(
            row_number,
            raw_row,
            ContactValidationStatus.INVALID,
            "missing name",
            phone_e164=phone.e164,
        )

    email_raw = _text(_lookup(raw_row, schema.source_for(AudienceField.EMAIL)))
    email: str | None = None
    if email_raw:
        if not _is_plausible_email(email_raw):
            return ValidatedRow(
                row_number,
                raw_row,
                ContactValidationStatus.INVALID,
                "invalid email address",
                phone_e164=phone.e164,
            )
        email = email_raw.casefold()

    company = _text(_lookup(raw_row, schema.source_for(AudienceField.COMPANY))) or None
    designation = _text(_lookup(raw_row, schema.source_for(AudienceField.DESIGNATION))) or None
    city = _text(_lookup(raw_row, schema.source_for(AudienceField.CITY))) or None

    if phone.e164 in seen:
        return ValidatedRow(
            row_number,
            raw_row,
            ContactValidationStatus.DUPLICATE,
            "duplicate phone number in this file",
            phone_e164=phone.e164,
            full_name=full_name,
            email=email,
            company=company,
            designation=designation,
            city=city,
        )

    return ValidatedRow(
        row_number,
        raw_row,
        ContactValidationStatus.VALID,
        None,
        phone_e164=phone.e164,
        full_name=full_name,
        email=email,
        company=company,
        designation=designation,
        city=city,
    )


def _lookup(raw_row: dict[str, object], source: str | None) -> object:
    """Fetch a column case-insensitively, tolerating stray header whitespace."""
    if source is None:
        return None
    wanted = source.strip().casefold()
    for key, value in raw_row.items():
        if key.strip().casefold() == wanted:
            return value
    return None


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()


def _is_plausible_email(value: str) -> bool:
    """A deliberately loose check.

    Full RFC validation rejects addresses that work in practice, and only delivery
    proves an address is real. This catches obvious mistakes without discarding rows
    the client considers valid.
    """
    if value.count("@") != 1 or any(character.isspace() for character in value):
        return False
    local, _, domain = value.partition("@")
    return bool(local) and "." in domain and not domain.startswith(".") and not domain.endswith(".")
