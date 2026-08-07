"""Tests for Excel conversion and in-workbook metadata-sheet detection.

Built against real ``.xlsx`` files written with openpyxl rather than mocks: the
behaviour that matters is how openpyxl actually reads an irregular sheet (blank
separator rows, a citation line, code-label lookups), and whether a documentation
sheet's messy layout can be split off before it fails the whole workbook.
"""

import sqlite3

import openpyxl
import pytest

from sql_browser import excel_loader as xl
from sql_browser.excel_loader import (
    ExcelLoadError,
    convert_with_metadata_detection,
    convert_xlsx_to_sqlite,
    extract_sheet_text,
)

# The documentation sheet mirrors the shape of the real bank-data workbook: a
# citation line, then two code lookups, separated by blank rows - not a clean
# rectangle, so it fails layout validation when treated as a table.
DOC_ROWS = [
    ("Source: Laramee 2026, open banking transaction dataset",),
    (None,),
    ("Category", "Description"),
    ("Groceries", "Supermarket and food shopping"),
    ("Salary", "Monthly employment income"),
    (None,),
    ("Transaction Type", "Meaning"),
    ("BGC", "Bank Giro Credit"),
    ("FPO", "Faster Payment Out"),
]


def _make_workbook(path, *, doc_sheet_name="Description of Categories"):
    """Write a workbook: one clean data sheet + one documentation sheet."""
    workbook = openpyxl.Workbook()
    data = workbook.active
    data.title = "Transactions"
    data.append(("Category", "Transaction Type", "Amount"))
    data.append(("Groceries", "FPO", -42.5))
    data.append(("Salary", "BGC", 2000.0))

    doc = workbook.create_sheet(doc_sheet_name)
    for row in DOC_ROWS:
        doc.append(row)

    workbook.save(path)
    return path


# --------------------------------------------------------------------------- #
# The whole-workbook-failure fix: skip_sheets
# --------------------------------------------------------------------------- #

def test_documentation_sheet_fails_the_whole_workbook_without_skip(tmp_path):
    """Regression: the doc sheet's blank rows sink the entire conversion."""
    book = _make_workbook(tmp_path / "book.xlsx")

    with pytest.raises(ExcelLoadError):
        convert_xlsx_to_sqlite(book)


def test_skipping_the_documentation_sheet_loads_only_the_data_table(tmp_path):
    book = _make_workbook(tmp_path / "book.xlsx")

    db_path = convert_xlsx_to_sqlite(book, skip_sheets={"Description of Categories"})
    try:
        connection = sqlite3.connect(str(db_path))
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        connection.close()
    finally:
        db_path.unlink(missing_ok=True)

    assert tables == {"Transactions"}


# --------------------------------------------------------------------------- #
# Detection heuristic + text extraction
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "title,expected",
    [
        ("Description of Categories", True),
        ("Glossary", True),
        ("Codes", True),
        ("Lookup", True),
        ("__metadata__", True),
        ("Transactions", False),
        ("Q1 Sales", False),
    ],
)
def test_looks_like_metadata_sheet(title, expected):
    assert xl._looks_like_metadata_sheet(title) is expected


def test_extract_sheet_text_flattens_and_preserves_separators():
    text = extract_sheet_text(DOC_ROWS)

    # Cells in a row are joined with ' | '.
    assert "Category | Description" in text
    assert "BGC | Bank Giro Credit" in text
    # A blank row becomes a paragraph break, not a dropped line.
    assert "\n\n" in text
    # No leading/trailing blank lines.
    assert text == text.strip()


# --------------------------------------------------------------------------- #
# The orchestrator
# --------------------------------------------------------------------------- #

def test_convert_with_metadata_detection_splits_off_the_doc_sheet(tmp_path):
    book = _make_workbook(tmp_path / "book.xlsx")

    db_path, warnings, metadata_text = convert_with_metadata_detection(book)
    try:
        connection = sqlite3.connect(str(db_path))
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        connection.close()
    finally:
        db_path.unlink(missing_ok=True)

    assert tables == {"Transactions"}
    assert "Description of Categories" in metadata_text
    assert "Bank Giro Credit" in metadata_text["Description of Categories"]
    assert any("Description of Categories" in w for w in warnings)


def test_detection_is_name_only_when_no_classifier(tmp_path):
    """A messy sheet with a data-ish name stays data when no classifier is given.

    'Notes' doesn't match the name heuristic; with ``classify=None`` there is no
    fallback, so an irregular sheet by that name is left as data - and its bad
    layout then surfaces as a normal ExcelLoadError, exactly as before.
    """
    book = _make_workbook(tmp_path / "book.xlsx", doc_sheet_name="Notes")

    with pytest.raises(ExcelLoadError):
        convert_with_metadata_detection(book)


def test_classifier_can_rescue_a_name_ambiguous_messy_sheet(tmp_path):
    """A classifier verdict of 'metadata' splits off an otherwise-fatal sheet."""
    book = _make_workbook(tmp_path / "book.xlsx", doc_sheet_name="Notes")

    def classify(title, sample):
        return "metadata" if title == "Notes" else None

    db_path, warnings, metadata_text = convert_with_metadata_detection(
        book, classify=classify
    )
    db_path.unlink(missing_ok=True)

    assert "Notes" in metadata_text
    assert any("Notes" in w for w in warnings)


def test_a_lone_documentation_sheet_is_kept_as_data(tmp_path):
    """The safety net: never leave a workbook with zero data sheets."""
    workbook = openpyxl.Workbook()
    only = workbook.active
    only.title = "Glossary"
    only.append(("Term", "Meaning"))
    only.append(("BGC", "Bank Giro Credit"))
    book = tmp_path / "lone.xlsx"
    workbook.save(book)

    db_path, warnings, metadata_text = convert_with_metadata_detection(book)
    try:
        connection = sqlite3.connect(str(db_path))
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        connection.close()
    finally:
        db_path.unlink(missing_ok=True)

    # It loaded as data; nothing was harvested as documentation.
    assert tables == {"Glossary"}
    assert metadata_text == {}

# Made with Bob
