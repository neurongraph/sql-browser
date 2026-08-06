"""Detects and corrects literal values in generated SQL that don't exist in the data.

LLMs sometimes guess a plausible-looking value for a filter (e.g.
Current_PIP_Status = 'Open', or status IN ('Opne', 'Closd')) that doesn't
actually appear in the column. This module finds such mismatches and lets the
caller resolve them - typically by asking the user to pick the real value -
before the query is executed.

Detection is AST-based (via sqlglot), so it is JOIN-aware (each column is
bound to its real table) and covers ``=``, ``!=``/``<>`` and ``IN (...)``
filters, string-escaping and quoted/qualified identifiers for free. If a query
can't be parsed we fall back to a simple regex over ``column = 'value'`` so
behaviour never regresses.

Candidate values come from the cached enum profile (see profiler.py) when
available, then a fuzzy LIKE search, then a small sample of distinct values.

The module exposes two shapes so both the blocking CLI prompt and the
stateless web picker can share the logic:

  - find_value_mismatches(sql, db) -> (sql_with_autofixes, [mismatch, ...])
        case-insensitive matches are auto-applied; ambiguous ones are returned.
  - apply_value_choices(sql, choices) -> sql
        apply user-picked replacements to a query (stateless; no db needed).
  - resolve_value_mismatches(sql, db, ask_choice) -> sql
        the CLI convenience wrapper that loops over ask_choice.
"""

import re
from typing import Callable, Dict, List, Optional, Tuple

import sqlglot
from sqlglot import exp

from .database import DatabaseInspector

# Fallback regex for when sqlglot can't parse the query: simple column = 'value'
_EQUALITY_PATTERN = re.compile(r"(\w+)\s*=\s*'([^']*)'")
_FROM_PATTERN = re.compile(r"\bFROM\s+\"?(\w+)\"?", re.IGNORECASE)

FALLBACK_SAMPLE_LIMIT = 20
MIN_FUZZY_WORD_LENGTH = 3

AskChoiceFn = Callable[[str, str, List[str]], Optional[str]]

# A detected mismatch, e.g.
#   {"column": "status", "attempted_value": "Opne", "candidates": ["Open", "Closed"]}
Mismatch = Dict[str, object]


# --------------------------------------------------------------------------- #
# Candidate lookup (shared by AST and regex paths)
# --------------------------------------------------------------------------- #

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


# --------------------------------------------------------------------------- #
# AST helpers
# --------------------------------------------------------------------------- #

def _safe_parse(sql: str) -> Optional[exp.Expression]:
    try:
        return sqlglot.parse_one(sql, read="sqlite")
    except Exception:
        return None


def _table_maps(
    expression: exp.Expression, db_inspector: DatabaseInspector
) -> Tuple[Dict[str, str], Dict[str, set]]:
    """Map aliases -> real table, and real table -> its column names.

    Only tables that actually exist in the database are included.
    """
    real_tables = set(db_inspector.get_tables())
    alias_to_table: Dict[str, str] = {}
    columns_by_table: Dict[str, set] = {}

    for tbl in expression.find_all(exp.Table):
        name = tbl.name
        if name not in real_tables:
            continue
        alias_to_table[tbl.alias_or_name] = name
        if name not in columns_by_table:
            columns_by_table[name] = {
                col["name"] for col in db_inspector.get_table_schema(name)
            }
    return alias_to_table, columns_by_table


def _resolve_table(
    column: exp.Column,
    alias_to_table: Dict[str, str],
    columns_by_table: Dict[str, set],
) -> Optional[str]:
    """Resolve which real table a column belongs to (JOIN-aware)."""
    qualifier = column.table
    if qualifier:
        return alias_to_table.get(qualifier)
    # Unqualified: only safe if exactly one referenced table has this column.
    owners = [t for t, cols in columns_by_table.items() if column.name in cols]
    return owners[0] if len(owners) == 1 else None


def _string_filter_sites(
    expression: exp.Expression,
) -> List[Tuple[exp.Column, exp.Literal]]:
    """Collect (column, string-literal) pairs from = / != / IN filters.

    LIKE is intentionally excluded: its patterns are meant to be partial
    matches, not exact values to correct against.
    """
    sites: List[Tuple[exp.Column, exp.Literal]] = []

    for node in expression.find_all(exp.EQ, exp.NEQ):
        left, right = node.left, node.right
        if isinstance(left, exp.Column) and isinstance(right, exp.Literal):
            col, lit = left, right
        elif isinstance(right, exp.Column) and isinstance(left, exp.Literal):
            col, lit = right, left
        else:
            continue
        if lit.is_string:
            sites.append((col, lit))

    for node in expression.find_all(exp.In):
        col = node.this
        if isinstance(col, exp.Column):
            for element in node.expressions:
                if isinstance(element, exp.Literal) and element.is_string:
                    sites.append((col, element))

    return sites


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def find_value_mismatches(
    sql: str, db_inspector: DatabaseInspector
) -> Tuple[str, List[Mismatch]]:
    """Find literal filter values that don't exist in their column.

    Case-insensitive matches (e.g. 'open' vs 'Open') are corrected in place and
    reflected in the returned SQL. Values with no clear match are returned as a
    list of mismatches for the caller to resolve interactively.

    Returns:
        (possibly-corrected SQL, list of unresolved mismatches)
    """
    expression = _safe_parse(sql)
    if expression is None:
        return _legacy_find_value_mismatches(sql, db_inspector)

    alias_to_table, columns_by_table = _table_maps(expression, db_inspector)
    if not columns_by_table:
        return sql, []

    mismatches: List[Mismatch] = []
    seen: set = set()
    changed = False

    for column_node, literal in _string_filter_sites(expression):
        table = _resolve_table(column_node, alias_to_table, columns_by_table)
        if not table:
            continue
        column = column_node.name
        if column not in columns_by_table[table]:
            continue

        value = literal.name
        candidates = _get_candidates(db_inspector, table, column, value)
        if not candidates or value in candidates:
            continue

        lower_map = {c.lower(): c for c in candidates}
        if value.lower() in lower_map:
            literal.replace(exp.Literal.string(lower_map[value.lower()]))
            changed = True
        elif (column, value) not in seen:
            seen.add((column, value))
            mismatches.append(
                {"column": column, "attempted_value": value, "candidates": candidates}
            )

    result_sql = expression.sql(dialect="sqlite") if changed else sql
    return result_sql, mismatches


def apply_value_choices(sql: str, choices: List[Dict[str, str]]) -> str:
    """Apply user-picked replacements to a query. Stateless (no db access).

    Args:
        sql: The query to rewrite
        choices: items shaped like
            {"column": ..., "attempted_value": ..., "chosen": ...}
            A falsy/absent "chosen" leaves that filter unchanged.

    Returns:
        The rewritten SQL.
    """
    active = [c for c in choices if c.get("chosen") and c["chosen"] != c.get("attempted_value")]
    if not active:
        return sql

    expression = _safe_parse(sql)
    if expression is None:
        # Best-effort string replacement if the query won't parse.
        for choice in active:
            sql = sql.replace(f"'{choice['attempted_value']}'", f"'{choice['chosen']}'")
        return sql

    sites = _string_filter_sites(expression)
    for choice in active:
        for column_node, literal in sites:
            if column_node.name == choice["column"] and literal.name == choice["attempted_value"]:
                literal.replace(exp.Literal.string(choice["chosen"]))
    return expression.sql(dialect="sqlite")


def resolve_value_mismatches(
    sql: str,
    db_inspector: DatabaseInspector,
    ask_choice: AskChoiceFn,
) -> str:
    """Correct guessed literal values, prompting via ask_choice when ambiguous.

    Case-insensitive matches are applied automatically. For each remaining
    mismatch, ask_choice(column, value, candidates) is called; a returned value
    replaces the literal, while None leaves it unchanged.

    Returns:
        The (possibly corrected) SQL query.
    """
    fixed_sql, mismatches = find_value_mismatches(sql, db_inspector)
    if not mismatches:
        return fixed_sql

    choices: List[Dict[str, str]] = []
    for mismatch in mismatches:
        replacement = ask_choice(
            mismatch["column"], mismatch["attempted_value"], mismatch["candidates"]
        )
        if replacement is not None:
            choices.append(
                {
                    "column": mismatch["column"],
                    "attempted_value": mismatch["attempted_value"],
                    "chosen": replacement,
                }
            )

    return apply_value_choices(fixed_sql, choices)


# --------------------------------------------------------------------------- #
# Regex fallback (used only when sqlglot can't parse the query)
# --------------------------------------------------------------------------- #

def extract_table_name(sql: str) -> Optional[str]:
    """Extract the first table name referenced in a FROM clause."""
    match = _FROM_PATTERN.search(sql)
    return match.group(1) if match else None


def extract_equality_filters(sql: str) -> List[Tuple[str, str]]:
    """Extract (column, value) pairs from simple equality filters in the SQL."""
    return _EQUALITY_PATTERN.findall(sql)


def _legacy_find_value_mismatches(
    sql: str, db_inspector: DatabaseInspector
) -> Tuple[str, List[Mismatch]]:
    """Single-table regex fallback matching the pre-sqlglot behaviour."""
    table = extract_table_name(sql)
    if not table or table not in db_inspector.get_tables():
        return sql, []

    columns = {col["name"] for col in db_inspector.get_table_schema(table)}
    corrected_sql = sql
    mismatches: List[Mismatch] = []
    seen: set = set()

    for column, value in extract_equality_filters(sql):
        if column not in columns:
            continue

        candidates = _get_candidates(db_inspector, table, column, value)
        if not candidates or value in candidates:
            continue

        lower_map = {c.lower(): c for c in candidates}
        if value.lower() in lower_map:
            corrected_sql = corrected_sql.replace(
                f"'{value}'", f"'{lower_map[value.lower()]}'"
            )
        elif (column, value) not in seen:
            seen.add((column, value))
            mismatches.append(
                {"column": column, "attempted_value": value, "candidates": candidates}
            )

    return corrected_sql, mismatches

# Made with Bob
