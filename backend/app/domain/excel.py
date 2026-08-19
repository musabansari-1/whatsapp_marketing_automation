"""Excel parsing (specification §6).

Turns uploaded bytes into headers and row dictionaries. Nothing here validates: that
is `app.domain.audience`, so parsing failures stay distinguishable from data problems.

§19 requires limiting uploaded files. The row cap is enforced while streaming rather
than after loading, so a file with a million rows is refused instead of exhausting
memory first.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from openpyxl import load_workbook


class ExcelParseError(Exception):
    """The upload is not a readable spreadsheet."""


class TooManyRowsError(ExcelParseError):
    """The file exceeds the configured row limit (§19)."""

    def __init__(self, limit: int) -> None:
        super().__init__(f"File exceeds the maximum of {limit} rows")
        self.limit = limit


@dataclass(frozen=True, slots=True)
class ParsedSheet:
    headers: list[str]
    rows: list[dict[str, object]]


def parse_excel(content: bytes, *, max_rows: int) -> ParsedSheet:
    """Parse the first worksheet into headers and row dictionaries.

    `read_only` and `data_only` matter: the first streams rather than building the
    whole document in memory, and the second yields cached formula *results* rather
    than formula text, since a cell containing `=A1` should contribute its value.
    """
    try:
        workbook = load_workbook(
            io.BytesIO(content), read_only=True, data_only=True, keep_links=False
        )
    except Exception as exc:  # noqa: BLE001 - openpyxl raises a variety of types
        raise ExcelParseError("The file could not be read as an .xlsx workbook") from exc

    try:
        worksheet = workbook.worksheets[0] if workbook.worksheets else None
        if worksheet is None:
            raise ExcelParseError("The workbook contains no worksheets")

        row_iterator = worksheet.iter_rows(values_only=True)

        try:
            header_row = next(row_iterator)
        except StopIteration:
            raise ExcelParseError("The worksheet is empty") from None

        headers = _clean_headers(header_row)
        if not headers:
            raise ExcelParseError("The first row must contain column headers")

        rows: list[dict[str, object]] = []
        for values in row_iterator:
            if _is_blank(values):
                # Trailing blank rows are an artifact of how spreadsheets are edited,
                # not data, and must not be reported as invalid rows.
                continue
            if len(rows) >= max_rows:
                raise TooManyRowsError(max_rows)
            rows.append(_to_dict(headers, values))

        return ParsedSheet(headers=headers, rows=rows)
    finally:
        workbook.close()


def _clean_headers(header_row: tuple[object, ...]) -> list[str]:
    """Header texts, positionally preserved.

    Empty headers become positional placeholders rather than being dropped, so the
    columns after a blank header still line up with their values.
    """
    headers: list[str] = []
    for index, value in enumerate(header_row):
        text = "" if value is None else str(value).strip()
        headers.append(text or f"column_{index + 1}")

    while headers and headers[-1].startswith("column_"):
        headers.pop()
    return headers


def _is_blank(values: tuple[object, ...]) -> bool:
    return all(value is None or (isinstance(value, str) and not value.strip()) for value in values)


def _to_dict(headers: list[str], values: tuple[object, ...]) -> dict[str, object]:
    """Zip headers with values, tolerating short and long rows.

    Spreadsheets routinely produce rows shorter than the header row; treating the
    missing cells as absent is correct, and extra cells beyond the headers are
    ignored because there is no column to attribute them to.
    """
    row: dict[str, object] = {}
    for index, header in enumerate(headers):
        row[header] = values[index] if index < len(values) else None
    return row
