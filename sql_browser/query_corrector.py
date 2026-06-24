"""Detects and corrects literal values in generated SQL that don't exist in the data.

LLMs sometimes guess a plausible-looking value for an equality filter (e.g.
Current_PIP_Status = 'Open') that doesn't actually appear in the column. This
module finds such mismatches and lets the caller resolve them - typically by
asking the user to pick the real value - before the query is executed.

When the database has been profiled (see profiler.py), the cached enum
values for low-cardinality columns are used directly. Otherwise (profiling
disabled, or a high-cardinality/numeric column with no enum profile), it
falls back to a LIKE-based substring search using words from the guessed
value, and finally to a small sample of the column's actual distinct values.
"""

import re
from typing import Callable, List, Optional, Tuple

from .database import DatabaseInspector

# Matches simple equality filters like: column = 'value'
_EQUALITY_PATTERN = re.compile(r"(\w+)\s*=\s*'([^']*)'")
_FROM_PATTERN = re.compile(r"\bFROM\s+\"?(\w+)\"?", re.IGNORECASE)

FALLBACK_SAMPLE_LIMIT = 20
MIN_FUZZY_WORD_LENGTH = 3

AskChoiceFn = Callable[[str, str, List[str]], Optional[str]]


def extract_table_name(sql: str) -> Optional[str]:
    """Extract the first table name referenced in a FROM clause."""
    match = _FROM_PATTERN.search(sql)
    return match.group(1) if match else None


def extract_equality_filters(sql: str) -> List[Tuple[str, str]]:
    """Extract (column, value) pairs from simple equality filters in the SQL."""
    return _EQUALITY_PATTERN.findall(sql)


def _escape_like(word: str) -> str:
    return word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _fuzzy_like_search(
    db_inspector: DatabaseInspector, table: str, column: str, value: str
) -> List[str]:
    """Search for actual values containing a word from the guessed value."""
    words = [w for w in re.split(r"\s+", value.strip()) if len(w) >= MIN_FUZZY_WORD_LENGTH]
    if not words:
        return []

    connection = db_inspector.connection
    if not connection:
        return []

    conditions = " OR ".join([f'"{column}" LIKE ? ESCAPE \'\\\''] * len(words))
    params = [f"%{_escape_like(w)}%" for w in words]

    cursor = connection.cursor()
    cursor.execute(
        f'SELECT DISTINCT "{column}" FROM "{table}" WHERE {conditions} LIMIT ?',
        (*params, FALLBACK_SAMPLE_LIMIT),
    )
    return [row[0] for row in cursor.fetchall()]


def _get_candidates(
    db_inspector: DatabaseInspector, table: str, column: str, value: str
) -> List[str]:
    """Find values to offer the user, preferring the cached profile if present."""
    profile_entry = db_inspector.profile.get(table, {}).get(column)
    if profile_entry and profile_entry.get("type") == "enum":
        return [str(v) for v in profile_entry["values"]]

    fuzzy_matches = _fuzzy_like_search(db_inspector, table, column, value)
    if fuzzy_matches:
        return [str(v) for v in fuzzy_matches]

    return [str(v) for v in db_inspector.get_distinct_values(table, column, limit=FALLBACK_SAMPLE_LIMIT)]


def resolve_value_mismatches(
    sql: str,
    db_inspector: DatabaseInspector,
    ask_choice: AskChoiceFn,
) -> str:
    """Replace literal values that don't exist in their column with a chosen value.

    For each equality filter (column = 'value') in the query, checks whether
    'value' actually occurs in that column. If not, calls ask_choice with the
    column name, the attempted value, and a list of real candidate values so
    the caller can pick a replacement (e.g. by prompting the user). If
    ask_choice returns None, the filter is left unchanged.

    Args:
        sql: The generated SQL query
        db_inspector: Connected database inspector
        ask_choice: Callback invoked as ask_choice(column, value, candidates)
            returning the replacement value, or None to leave it as-is

    Returns:
        The (possibly corrected) SQL query
    """
    table = extract_table_name(sql)
    if not table or table not in db_inspector.get_tables():
        return sql

    columns = {col["name"] for col in db_inspector.get_table_schema(table)}
    corrected_sql = sql

    for column, value in extract_equality_filters(sql):
        if column not in columns:
            continue

        candidates = _get_candidates(db_inspector, table, column, value)
        if not candidates:
            continue
        if value in candidates:
            continue

        lower_map = {c.lower(): c for c in candidates}
        if value.lower() in lower_map:
            replacement = lower_map[value.lower()]
        else:
            replacement = ask_choice(column, value, candidates)

        if replacement is not None and replacement != value:
            corrected_sql = corrected_sql.replace(f"'{value}'", f"'{replacement}'")

    return corrected_sql

# Made with Bob
