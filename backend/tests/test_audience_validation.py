"""Audience validation tests (specification §6, [D4], [D20]).

Pure rules, no database. The schema used here is the development placeholder, which
is the point: these tests must keep passing when the client's real column names
replace it, because nothing in the pipeline is tied to particular names.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.domain.audience import (
    AudienceColumnSchema,
    AudienceField,
    ColumnMapping,
    ValidationSummary,
    find_missing_columns,
    validate_rows,
)
from app.domain.enums import ContactValidationStatus
from app.domain.phone import normalize_phone

SCHEMA = AudienceColumnSchema(
    columns=[
        ColumnMapping(source="Phone", field=AudienceField.PHONE, required=True),
        ColumnMapping(source="Name", field=AudienceField.FULL_NAME),
        ColumnMapping(source="Email", field=AudienceField.EMAIL),
    ]
)

# A schema with entirely different names, standing in for whatever the client sends.
CLIENT_SCHEMA = AudienceColumnSchema(
    columns=[
        ColumnMapping(source="Mobile Number", field=AudienceField.PHONE, required=True),
        ColumnMapping(source="Customer Name", field=AudienceField.FULL_NAME),
    ]
)


def _validate(
    rows: list[dict[str, object]], schema: AudienceColumnSchema = SCHEMA
) -> ValidationSummary:
    return validate_rows(rows, schema=schema, default_region="IN")


class TestPhoneNormalization:
    def test_national_number_uses_the_default_region(self) -> None:
        """[D4]: Indian numbers by default."""
        assert normalize_phone("9876543210", default_region="IN").e164 == "+919876543210"

    def test_international_format_is_preserved(self) -> None:
        assert normalize_phone("+919876543210", default_region="IN").e164 == "+919876543210"

    @pytest.mark.parametrize(
        "raw",
        ["+91 98765 43210", "098765 43210", "+91-98765-43210", "  +919876543210  "],
    )
    def test_spellings_converge_on_one_value(self, raw: str) -> None:
        """Deduplication and suppression both depend on this."""
        assert normalize_phone(raw, default_region="IN").e164 == "+919876543210"

    def test_integer_cell_is_accepted(self) -> None:
        """Excel stores a phone column of digits as a number."""
        assert normalize_phone(919876543210, default_region="IN").e164 == "+919876543210"

    def test_float_cell_does_not_gain_a_decimal_point(self) -> None:
        """A float cell would otherwise arrive as '919876543210.0' and fail for a
        reason that looks like bad data rather than a spreadsheet artifact."""
        assert normalize_phone(919876543210.0, default_region="IN").e164 == "+919876543210"

    @pytest.mark.parametrize("raw", [None, "", "   ", "not a phone", "12345", True])
    def test_unusable_values_are_reported_not_guessed(self, raw: object) -> None:
        result = normalize_phone(raw, default_region="IN")
        assert result.ok is False
        assert result.error

    def test_region_is_configurable(self) -> None:
        assert normalize_phone("2015550123", default_region="US").e164 == "+12015550123"


class TestColumnSchema:
    def test_phone_column_is_mandatory(self) -> None:
        """[D11] makes the phone the WhatsApp identity, so a schema without one
        describes a file this system cannot use."""
        with pytest.raises(ValidationError, match="must map a phone column"):
            AudienceColumnSchema(
                columns=[ColumnMapping(source="Name", field=AudienceField.FULL_NAME)]
            )

    def test_phone_column_must_be_required(self) -> None:
        with pytest.raises(ValidationError, match="phone column must be required"):
            AudienceColumnSchema(
                columns=[ColumnMapping(source="Phone", field=AudienceField.PHONE, required=False)]
            )

    def test_duplicate_field_mappings_are_rejected(self) -> None:
        with pytest.raises(ValidationError, match="duplicate field mappings"):
            AudienceColumnSchema(
                columns=[
                    ColumnMapping(source="Phone", field=AudienceField.PHONE, required=True),
                    ColumnMapping(source="Mobile", field=AudienceField.PHONE, required=True),
                ]
            )

    def test_empty_schema_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AudienceColumnSchema(columns=[])


class TestRequiredColumns:
    def test_missing_required_column_is_detected(self) -> None:
        assert find_missing_columns(["Name", "Email"], SCHEMA) == ["Phone"]

    def test_present_columns_pass(self) -> None:
        assert find_missing_columns(["Phone", "Name", "Email"], SCHEMA) == []

    def test_header_matching_ignores_case_and_whitespace(self) -> None:
        """Headers are typed by humans."""
        assert find_missing_columns(["  phone  ", "NAME"], SCHEMA) == []

    def test_optional_columns_may_be_absent(self) -> None:
        assert find_missing_columns(["Phone"], SCHEMA) == []


class TestRowValidation:
    def test_valid_row(self) -> None:
        summary = _validate(
            [{"Phone": "9876543210", "Name": "Rahul", "Email": "rahul@example.com"}]
        )
        row = summary.rows[0]

        assert row.status is ContactValidationStatus.VALID
        assert row.phone_e164 == "+919876543210"
        assert row.full_name == "Rahul"
        assert row.email == "rahul@example.com"
        assert summary.valid == 1

    def test_row_numbers_are_one_based(self) -> None:
        """They refer to data rows as a person counts them, not spreadsheet rows."""
        summary = _validate([{"Phone": "9876543210"}, {"Phone": "9876543211"}])
        assert [row.row_number for row in summary.rows] == [1, 2]

    def test_invalid_phone_is_recorded_not_dropped(self) -> None:
        """§6 requires the preview to show what was rejected and why."""
        summary = _validate([{"Phone": "nonsense", "Name": "X"}])
        row = summary.rows[0]

        assert row.status is ContactValidationStatus.INVALID
        assert row.error
        assert summary.invalid == 1
        assert len(summary.rows) == 1

    def test_missing_phone_is_invalid(self) -> None:
        summary = _validate([{"Phone": None, "Name": "X"}])
        assert summary.rows[0].status is ContactValidationStatus.INVALID

    def test_invalid_email_invalidates_the_row(self) -> None:
        summary = _validate([{"Phone": "9876543210", "Email": "not-an-email"}])
        assert summary.rows[0].status is ContactValidationStatus.INVALID
        assert summary.rows[0].error == "invalid email address"

    def test_email_is_optional(self) -> None:
        summary = _validate([{"Phone": "9876543210", "Email": None}])
        assert summary.rows[0].status is ContactValidationStatus.VALID
        assert summary.rows[0].email is None

    def test_email_is_lowercased(self) -> None:
        summary = _validate([{"Phone": "9876543210", "Email": "Rahul@Example.COM"}])
        assert summary.rows[0].email == "rahul@example.com"

    def test_unmapped_columns_are_preserved(self) -> None:
        """No uploaded data is silently discarded."""
        summary = _validate([{"Phone": "9876543210", "City": "Mumbai"}])
        assert summary.rows[0].raw_row["City"] == "Mumbai"

    def test_schema_is_data_not_code(self) -> None:
        """[D20]: swapping in the client's column names must need no code change."""
        summary = _validate(
            [{"Mobile Number": "9876543210", "Customer Name": "Priya"}], CLIENT_SCHEMA
        )
        assert summary.rows[0].status is ContactValidationStatus.VALID
        assert summary.rows[0].phone_e164 == "+919876543210"
        assert summary.rows[0].full_name == "Priya"


class TestDuplicateDetection:
    def test_repeated_number_marks_later_rows_duplicate(self) -> None:
        summary = _validate([{"Phone": "9876543210"}, {"Phone": "9876543210"}])

        assert summary.rows[0].status is ContactValidationStatus.VALID
        assert summary.rows[1].status is ContactValidationStatus.DUPLICATE
        assert summary.valid == 1
        assert summary.duplicate == 1

    def test_duplicates_are_found_after_normalization(self) -> None:
        """Two spellings of one number are the same number."""
        summary = _validate(
            [{"Phone": "9876543210"}, {"Phone": "+91 98765 43210"}, {"Phone": "098765 43210"}]
        )
        statuses = [row.status for row in summary.rows]
        assert statuses == [
            ContactValidationStatus.VALID,
            ContactValidationStatus.DUPLICATE,
            ContactValidationStatus.DUPLICATE,
        ]

    def test_first_occurrence_wins(self) -> None:
        """Outcome depends on file order, not processing order."""
        summary = _validate(
            [{"Phone": "9876543210", "Name": "First"}, {"Phone": "9876543210", "Name": "Second"}]
        )
        assert summary.rows[0].full_name == "First"

    def test_an_invalid_row_does_not_reserve_a_number(self) -> None:
        """A row rejected for a bad email must not make a later good row a duplicate."""
        summary = _validate(
            [
                {"Phone": "9876543210", "Email": "bad"},
                {"Phone": "9876543210", "Email": "ok@example.com"},
            ]
        )
        assert summary.rows[0].status is ContactValidationStatus.INVALID
        assert summary.rows[1].status is ContactValidationStatus.VALID

    def test_distinct_numbers_are_all_valid(self) -> None:
        summary = _validate([{"Phone": "9876543210"}, {"Phone": "9876543211"}])
        assert summary.valid == 2
        assert summary.duplicate == 0


class TestSummaryCounts:
    def test_counts_partition_the_rows(self) -> None:
        summary = _validate(
            [
                {"Phone": "9876543210"},
                {"Phone": "9876543210"},
                {"Phone": "rubbish"},
                {"Phone": "9876543211"},
            ]
        )
        assert summary.total == 4
        assert summary.valid == 2
        assert summary.duplicate == 1
        assert summary.invalid == 1
        assert summary.valid + summary.duplicate + summary.invalid == summary.total

    def test_empty_input(self) -> None:
        summary = _validate([])
        assert summary.total == 0
        assert summary.rows == []
