"""Excel parsing tests (specification §6, §19)."""

from __future__ import annotations

import io
from collections.abc import Sequence
from datetime import datetime

import pytest
from openpyxl import Workbook

from app.domain.excel import ExcelParseError, TooManyRowsError, parse_excel


def make_xlsx(rows: Sequence[Sequence[object]]) -> bytes:
    """Build a real .xlsx in memory, so the parser is exercised against the actual
    format rather than a mock."""
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    for row in rows:
        sheet.append(list(row))
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


class TestParsing:
    def test_headers_and_rows(self) -> None:
        content = make_xlsx([["Phone", "Name"], ["9876543210", "Rahul"]])
        sheet = parse_excel(content, max_rows=100)

        assert sheet.headers == ["Phone", "Name"]
        assert sheet.rows == [{"Phone": "9876543210", "Name": "Rahul"}]

    def test_header_whitespace_is_trimmed(self) -> None:
        sheet = parse_excel(make_xlsx([["  Phone  "], ["9876543210"]]), max_rows=100)
        assert sheet.headers == ["Phone"]

    def test_numeric_cells_keep_their_type(self) -> None:
        """Normalization handles the int/float shapes; the parser must not stringify
        and lose that information."""
        sheet = parse_excel(make_xlsx([["Phone"], [919876543210]]), max_rows=100)
        assert sheet.rows[0]["Phone"] == 919876543210

    def test_blank_rows_are_skipped(self) -> None:
        """Trailing blanks are an editing artifact, not data, and must not be
        reported as invalid rows."""
        content = make_xlsx([["Phone"], ["9876543210"], [None], ["", None], ["9876543211"]])
        sheet = parse_excel(content, max_rows=100)
        assert len(sheet.rows) == 2

    def test_short_rows_are_padded(self) -> None:
        """Spreadsheets routinely omit trailing cells."""
        content = make_xlsx([["Phone", "Name", "Email"], ["9876543210"]])
        sheet = parse_excel(content, max_rows=100)
        assert sheet.rows[0] == {"Phone": "9876543210", "Name": None, "Email": None}

    def test_datetime_cells_survive(self) -> None:
        """Excel cannot store a timezone, so date cells arrive naive. They are kept
        rather than dropped; the repository stringifies them for JSONB storage."""
        naive = datetime(2026, 1, 1, 9, 30)  # noqa: DTZ001 - Excel dates carry no zone
        content = make_xlsx([["Phone", "Joined"], ["9876543210", naive]])
        sheet = parse_excel(content, max_rows=100)
        assert sheet.rows[0]["Joined"] == naive

    def test_only_the_first_worksheet_is_read(self) -> None:
        workbook = Workbook()
        first = workbook.active
        assert first is not None
        first.append(["Phone"])
        first.append(["9876543210"])
        second = workbook.create_sheet("Other")
        second.append(["Phone"])
        second.append(["9999999999"])

        buffer = io.BytesIO()
        workbook.save(buffer)
        sheet = parse_excel(buffer.getvalue(), max_rows=100)

        assert len(sheet.rows) == 1


class TestLimitsAndFailures:
    def test_row_limit_is_enforced(self) -> None:
        """§19: limit uploaded files. Enforced while streaming, so an oversized file
        is refused rather than exhausting memory first."""
        content = make_xlsx([["Phone"], *[[f"98765432{i:02d}"] for i in range(10)]])
        with pytest.raises(TooManyRowsError):
            parse_excel(content, max_rows=5)

    def test_a_file_at_the_limit_is_accepted(self) -> None:
        content = make_xlsx([["Phone"], *[[f"98765432{i:02d}"] for i in range(5)]])
        assert len(parse_excel(content, max_rows=5).rows) == 5

    def test_non_xlsx_content_is_rejected(self) -> None:
        with pytest.raises(ExcelParseError):
            parse_excel(b"this is not a spreadsheet", max_rows=100)

    def test_empty_bytes_are_rejected(self) -> None:
        with pytest.raises(ExcelParseError):
            parse_excel(b"", max_rows=100)

    def test_empty_worksheet_is_rejected(self) -> None:
        with pytest.raises(ExcelParseError, match="empty"):
            parse_excel(make_xlsx([]), max_rows=100)

    def test_headers_without_data_parse_to_no_rows(self) -> None:
        """Distinguishable from a broken file: the caller decides what to do."""
        sheet = parse_excel(make_xlsx([["Phone", "Name"]]), max_rows=100)
        assert sheet.headers == ["Phone", "Name"]
        assert sheet.rows == []
