"""Convert multi-sheet Excel workbooks into temporary SQLite databases."""

import os
import re
import sqlite3
import tempfile
import zipfile
from pathlib import Path
from typing import Dict, List, Set

import openpyxl
from openpyxl.utils import get_column_letter
from openpyxl.utils.exceptions import InvalidFileException

EXCEL_EXTENSIONS = {".xlsx", ".xlsm"}


class ExcelLoadError(Exception):
    """Raised when an Excel workbook can't be converted into a database.

    Carries a list of human-readable issues (rather than just one message)
    so the caller can show the user everything that needs fixing in their
    spreadsheet at once, instead of one error per re-attempt.
    """

    def __init__(self, issues: List[str]):
        self.issues = issues
        super().__init__("\n".join(issues))


def _sanitize_identifier(name: str, used: Set[str]) -> str:
    """Turn an arbitrary sheet/column name into a safe, unique SQL identifier."""
    sanitized = re.sub(r"\W+", "_", name.strip()) or "column"
    if sanitized[0].isdigit():
        sanitized = f"_{sanitized}"

    base = sanitized
    counter = 1
    while sanitized.lower() in used:
        sanitized = f"{base}_{counter}"
        counter += 1
    used.add(sanitized.lower())
    return sanitized


def _infer_column_type(values: List) -> str:
    """Infer a SQLite column type from a sample of cell values."""
    saw_float = False
    saw_value = False
    for value in values:
        if value is None or value == "":
            continue
        saw_value = True
        if isinstance(value, bool):
            return "TEXT"
        if isinstance(value, int):
            continue
        if isinstance(value, float):
            saw_float = True
            continue
        return "TEXT"
    if not saw_value:
        return "TEXT"
    return "REAL" if saw_float else "INTEGER"


def _is_blank_value(value) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def _is_blank_row(row: tuple) -> bool:
    return all(_is_blank_value(cell) for cell in row)


def _check_layout(sheet_title: str, all_rows: List[tuple]):
    """Find rows/columns that aren't laid out as a clean table.

    Returns a tuple of (issues, header_row_index, last_data_row_index). When
    issues is non-empty, the row/column indices should be ignored.
    """
    non_blank_row_indices = [i for i, row in enumerate(all_rows) if not _is_blank_row(row)]
    header_idx = non_blank_row_indices[0]
    last_idx = non_blank_row_indices[-1]

    issues: List[str] = []

    if header_idx > 0:
        issues.append(
            f"Sheet '{sheet_title}': data doesn't start at row 1 (row(s) "
            f"1-{header_idx} are blank). Move the header row to row 1."
        )

    header = all_rows[header_idx]
    non_blank_col_indices = [i for i, cell in enumerate(header) if not _is_blank_value(cell)]
    if non_blank_col_indices:
        first_col, last_col = non_blank_col_indices[0], non_blank_col_indices[-1]

        if first_col > 0:
            issues.append(
                f"Sheet '{sheet_title}': data doesn't start at column A (the header "
                f"row is blank up to column {get_column_letter(first_col)}). Move "
                "the data to column A."
            )

        gap_cols = [i for i in range(first_col, last_col + 1) if i not in non_blank_col_indices]
        if gap_cols:
            letters = ", ".join(get_column_letter(i + 1) for i in gap_cols)
            issues.append(
                f"Sheet '{sheet_title}': header row has blank column(s) at "
                f"{letters}, between columns {get_column_letter(first_col + 1)} and "
                f"{get_column_letter(last_col + 1)}. Remove the gap so the header "
                "columns are contiguous."
            )

    blank_data_rows = [
        i for i in range(header_idx + 1, last_idx + 1) if _is_blank_row(all_rows[i])
    ]
    if blank_data_rows:
        row_numbers = ", ".join(str(i + 1) for i in blank_data_rows)
        issues.append(
            f"Sheet '{sheet_title}': blank row(s) at row(s) {row_numbers} in the "
            "middle of the data. Remove empty rows so the data is contiguous."
        )

    return issues, header_idx, last_idx


def _find_duplicate_headers(sheet_title: str, header: tuple) -> List[str]:
    """Return issue strings for any column header that appears more than once."""
    locations_by_name: Dict[str, List[str]] = {}
    for i, cell in enumerate(header):
        if cell is None or str(cell).strip() == "":
            continue
        key = str(cell).strip().lower()
        locations_by_name.setdefault(key, []).append(get_column_letter(i + 1))

    issues = []
    for name, locations in locations_by_name.items():
        if len(locations) > 1:
            issues.append(
                f"Sheet '{sheet_title}': duplicate column header '{name}' found in "
                f"columns {', '.join(locations)}. Rename the columns so each header "
                "is unique, then try again."
            )
    return issues


def convert_xlsx_to_sqlite(xlsx_path: Path) -> Path:
    """Convert an Excel workbook into a temporary SQLite database.

    Each non-empty worksheet becomes a table named after the sheet, with the
    first row treated as the column headers.

    Args:
        xlsx_path: Path to the .xlsx/.xlsm file

    Returns:
        Path to the generated temporary SQLite database file

    Raises:
        ExcelLoadError: If the workbook can't be opened, or contains issues
            (e.g. duplicate column headers, no usable data) that need to be
            fixed before it can be loaded. `.issues` lists every problem found.
    """
    try:
        workbook = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    except (zipfile.BadZipFile, InvalidFileException, KeyError, OSError) as e:
        raise ExcelLoadError(
            [
                f"Could not open '{xlsx_path.name}': {e}. Make sure the file is a "
                "valid, non-corrupted .xlsx/.xlsm workbook and is not "
                "password-protected."
            ]
        ) from e

    fd, tmp_name = tempfile.mkstemp(suffix=".db", prefix="sql_browser_")
    os.close(fd)
    db_path = Path(tmp_name)

    connection = sqlite3.connect(str(db_path))
    used_table_names: Set[str] = set()
    issues: List[str] = []

    try:
        for sheet in workbook.worksheets:
            all_rows = list(sheet.iter_rows(values_only=True))
            if not all_rows or all(_is_blank_row(row) for row in all_rows):
                continue

            layout_issues, header_idx, last_idx = _check_layout(sheet.title, all_rows)
            if layout_issues:
                issues.extend(layout_issues)
                continue

            header = all_rows[header_idx]
            data_rows = all_rows[header_idx + 1 : last_idx + 1]

            duplicate_issues = _find_duplicate_headers(sheet.title, header)
            if duplicate_issues:
                issues.extend(duplicate_issues)
                continue

            try:
                used_column_names: Set[str] = set()
                columns = [
                    _sanitize_identifier(
                        str(cell) if cell is not None else f"column_{i}",
                        used_column_names,
                    )
                    for i, cell in enumerate(header)
                ]

                col_types = [
                    _infer_column_type(
                        [row[i] if i < len(row) else None for row in data_rows]
                    )
                    for i in range(len(columns))
                ]

                table_name = _sanitize_identifier(sheet.title, used_table_names)
                columns_sql = ", ".join(
                    f'"{name}" {col_type}' for name, col_type in zip(columns, col_types)
                )
                connection.execute(f'CREATE TABLE "{table_name}" ({columns_sql})')

                if data_rows:
                    normalized_rows = [
                        tuple(row[i] if i < len(row) else None for i in range(len(columns)))
                        for row in data_rows
                    ]
                    placeholders = ", ".join(["?"] * len(columns))
                    connection.executemany(
                        f'INSERT INTO "{table_name}" VALUES ({placeholders})',
                        normalized_rows,
                    )
            except sqlite3.Error as e:
                issues.append(
                    f"Sheet '{sheet.title}': failed to load into a table ({e}). "
                    "Check that every row has the same number of columns as the "
                    "header row, and that a column doesn't mix incompatible data "
                    "(e.g. text mixed with numbers in a way that confuses type "
                    "detection)."
                )

        connection.commit()
    finally:
        connection.close()
        workbook.close()

    if issues:
        db_path.unlink(missing_ok=True)
        raise ExcelLoadError(issues)

    if not used_table_names:
        db_path.unlink(missing_ok=True)
        raise ExcelLoadError(
            [
                f"No usable data found in any sheet of '{xlsx_path.name}'. Every "
                "sheet is empty or has no header row. Add at least one sheet with "
                "a header row and some data, then try again."
            ]
        )

    return db_path
