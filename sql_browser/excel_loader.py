"""Convert multi-sheet Excel workbooks into temporary SQLite databases."""

import os
import re
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

import openpyxl
from openpyxl.utils import get_column_letter
from openpyxl.utils.exceptions import InvalidFileException

EXCEL_EXTENSIONS = {".xlsx", ".xlsm"}

# A sheet named exactly this is always treated as documentation, never a table.
EXPLICIT_METADATA_SHEET = "__metadata__"

# Sheets whose name hints at documentation rather than data. Deliberately cheap
# and LLM-free: the point is to catch the common "Description of Categories" /
# "Glossary" / "Codes" sheets by name before spending a classifier call. Note
# that a name match alone is not decisive — plan_sheets() also refuses to leave
# a workbook with no data sheet, so a lone "descriptions" sheet stays data.
_METADATA_NAME_RE = re.compile(r"meta|description|glossary|codes|lookup", re.IGNORECASE)

# Signature of an optional classifier the caller may pass to resolve a
# name-ambiguous, badly-laid-out sheet: (title, sample_text) -> verdict, where
# verdict is "data", "metadata", or None ("don't know" — treated as data).
SheetClassifier = Callable[[str, str], Optional[str]]


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


def _open_workbook(xlsx_path: Path):
    """Open a workbook read-only, translating failures into ExcelLoadError."""
    try:
        return openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    except (zipfile.BadZipFile, InvalidFileException, KeyError, OSError) as e:
        raise ExcelLoadError(
            [
                f"Could not open '{xlsx_path.name}': {e}. Make sure the file is a "
                "valid, non-corrupted .xlsx/.xlsm workbook and is not "
                "password-protected."
            ]
        ) from e


def _looks_like_metadata_sheet(title: str) -> bool:
    """Cheap, LLM-free guess that a sheet is documentation rather than data."""
    normalized = title.strip().lower()
    if normalized == EXPLICIT_METADATA_SHEET:
        return True
    return _METADATA_NAME_RE.search(title) is not None


def extract_sheet_text(all_rows: List[tuple]) -> str:
    """Flatten a sheet's cells into free-form text for prose ingestion.

    Non-blank cells in a row are joined left-to-right with ' | '; a fully blank
    row becomes a paragraph break. This is the raw material an LLM later parses
    into a prose metadata section, so it keeps the human's row/column grouping
    rather than trying to impose a table structure on an irregular sheet.
    """
    lines: List[str] = []
    prev_blank = True  # suppress leading blank lines
    for row in all_rows:
        cells = [str(cell).strip() for cell in row if not _is_blank_value(cell)]
        if not cells:
            if not prev_blank:
                lines.append("")
            prev_blank = True
            continue
        lines.append(" | ".join(cells))
        prev_blank = False

    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


@dataclass
class SheetPlan:
    """How a single worksheet should be handled during conversion.

    role is "data" (load as a table) or "metadata" (skip the table, harvest its
    text). text is populated only for metadata sheets. reason is a short,
    user-facing explanation of why the sheet was classified this way.
    """

    title: str
    role: str
    reason: str
    text: Optional[str] = None


def plan_sheets(
    workbook, classify: Optional[SheetClassifier] = None
) -> List[SheetPlan]:
    """Decide, per sheet, whether it is data or documentation.

    The name heuristic decides first. A sheet that is name-ambiguous *and* fails
    the clean-table layout check may consult the optional `classify` callback
    (typically an LLM); anything still uncertain stays "data" — a tidy
    rectangular sheet is always data. As a final safety net, a workbook is never
    left with zero data sheets: if every non-empty sheet looked like metadata,
    they are all reverted to data.

    Args:
        workbook: An open openpyxl workbook.
        classify: Optional (title, sample) -> "data"|"metadata"|None callback,
            consulted only for the ambiguous-and-messy case.

    Returns:
        One SheetPlan per worksheet, in workbook order.
    """
    plans: List[SheetPlan] = []
    for sheet in workbook.worksheets:
        title = sheet.title
        all_rows = list(sheet.iter_rows(values_only=True))
        if not all_rows or all(_is_blank_row(row) for row in all_rows):
            # Empty sheets never become tables anyway; label them data so they
            # don't count against the "must have a data sheet" guard below.
            plans.append(SheetPlan(title, "data", "empty sheet"))
            continue

        role, reason = "data", "clean table layout"
        if _looks_like_metadata_sheet(title):
            role, reason = "metadata", "sheet name looks like documentation"
        else:
            layout_issues, _, _ = _check_layout(title, all_rows)
            if layout_issues and classify is not None:
                try:
                    verdict = classify(title, extract_sheet_text(all_rows)[:2000])
                except Exception:
                    verdict = None  # graceful degradation: unknown -> data
                if verdict == "metadata":
                    role, reason = "metadata", "classified as documentation"

        text = extract_sheet_text(all_rows) if role == "metadata" else None
        plans.append(SheetPlan(title, role, reason, text))

    non_empty = [p for p in plans if p.reason != "empty sheet"]
    if non_empty and all(p.role == "metadata" for p in non_empty):
        # A workbook is data first. If detection swallowed every sheet, back off
        # rather than convert to an empty database.
        for plan in plans:
            if plan.role == "metadata":
                plan.role = "data"
                plan.reason = "only sheet(s) available; loaded as data"
                plan.text = None

    return plans


def convert_xlsx_to_sqlite(
    xlsx_path: Path, skip_sheets: Optional[Set[str]] = None
) -> Path:
    """Convert an Excel workbook into a temporary SQLite database.

    Each non-empty worksheet becomes a table named after the sheet, with the
    first row treated as the column headers.

    Args:
        xlsx_path: Path to the .xlsx/.xlsm file
        skip_sheets: Sheet titles to leave out entirely (e.g. sheets detected as
            documentation). Skipped before layout validation, so a metadata
            sheet's irregular layout no longer fails the whole workbook. Default
            None preserves the original every-sheet-is-a-table behavior.

    Returns:
        Path to the generated temporary SQLite database file

    Raises:
        ExcelLoadError: If the workbook can't be opened, or contains issues
            (e.g. duplicate column headers, no usable data) that need to be
            fixed before it can be loaded. `.issues` lists every problem found.
    """
    skip_sheets = skip_sheets or set()
    workbook = _open_workbook(xlsx_path)

    fd, tmp_name = tempfile.mkstemp(suffix=".db", prefix="sql_browser_")
    os.close(fd)
    db_path = Path(tmp_name)

    connection = sqlite3.connect(str(db_path))
    used_table_names: Set[str] = set()
    issues: List[str] = []

    try:
        for sheet in workbook.worksheets:
            if sheet.title in skip_sheets:
                continue

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


def convert_with_metadata_detection(
    xlsx_path: Path, classify: Optional[SheetClassifier] = None
) -> Tuple[Path, List[str], Dict[str, str]]:
    """Convert a workbook, splitting off any sheets that look like documentation.

    This is the metadata-aware front door to `convert_xlsx_to_sqlite`. Sheets
    classified as documentation are skipped during conversion (so their
    irregular layout can't fail the whole workbook) and their text is harvested
    for later prose parsing. Genuine *data*-sheet problems still raise
    `ExcelLoadError` exactly as before.

    Args:
        xlsx_path: Path to the .xlsx/.xlsm file
        classify: Optional classifier for name-ambiguous, messy sheets; see
            `plan_sheets`. When None, detection is name-heuristic only.

    Returns:
        A tuple of (db_path, warnings, metadata_text) where metadata_text maps
        each detected documentation sheet's title to its extracted free-form
        text, and warnings explains which sheets were treated as documentation.

    Raises:
        ExcelLoadError: If the workbook can't be opened or a data sheet has
            issues that need fixing.
    """
    workbook = _open_workbook(xlsx_path)
    try:
        plans = plan_sheets(workbook, classify=classify)
    finally:
        workbook.close()

    skip_sheets = {p.title for p in plans if p.role == "metadata"}
    metadata_text = {
        p.title: p.text for p in plans if p.role == "metadata" and p.text
    }
    warnings = [
        f"Sheet '{p.title}' treated as documentation, not loaded as a table "
        f"({p.reason})."
        for p in plans
        if p.role == "metadata"
    ]

    db_path = convert_xlsx_to_sqlite(xlsx_path, skip_sheets=skip_sheets)
    return db_path, warnings, metadata_text
