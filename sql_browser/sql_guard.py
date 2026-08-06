"""Deterministic SQL validation and repair layer built on sqlglot.

This sits between LLM generation and execution. Local models make *structured*
mistakes - wrong dialect functions, hallucinated columns, unbounded result
sets - that a parser can catch or fix without another round-trip. The pipeline:

    transpile to SQLite -> validate read-only -> check against real schema
    -> (caller may repair) -> enforce a row limit -> execute

Every step degrades gracefully: if sqlglot can't parse a query we fall back to
the original string rather than block an otherwise-valid query. Execution
remains the ultimate arbiter of correctness.
"""

from typing import Dict, List

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError
from sqlglot.optimizer.qualify import qualify

from .database import DatabaseInspector
from .logger import get_logger

logger = get_logger(__name__)

DEFAULT_ROW_LIMIT = 1000

# Statement/expression types that make a query more than a read. If any of
# these appear anywhere in the parsed tree, the query is rejected.
_FORBIDDEN = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Alter,
    exp.Create,
    exp.Command,  # catch-all for unparsed/unknown statements (ATTACH, VACUUM, ...)
    exp.Pragma,
    exp.Set,
)


def build_schema(db_inspector: DatabaseInspector) -> Dict[str, Dict[str, str]]:
    """Build a sqlglot schema mapping: {table: {column: type}}.

    Used to bind a query's columns to real tables so hallucinated
    tables/columns can be detected before execution.
    """
    schema: Dict[str, Dict[str, str]] = {}
    for table in db_inspector.get_tables():
        schema[table] = {
            col["name"]: (col["type"] or "TEXT")
            for col in db_inspector.get_table_schema(table)
        }
    return schema


def transpile_to_sqlite(sql: str) -> str:
    """Normalize a query to SQLite syntax.

    Tries to parse as SQLite first so a valid SQLite query is never altered.
    Only if that fails do we reinterpret under a more permissive dialect
    (MySQL) to rescue cross-dialect idioms. On total failure the original
    string is returned unchanged.
    """
    for read in ("sqlite", "mysql", None):
        try:
            out = sqlglot.transpile(sql, read=read, write="sqlite")
            if out:
                return out[0]
        except Exception:
            continue
    return sql


def is_read_only(sql: str) -> bool:
    """Return True only for a single read-only SELECT/UNION statement.

    Correct where a keyword blacklist is not: it won't false-reject a column
    named ``created_at`` or ``updated_at``, and it rejects stacked statements
    like ``SELECT ...; DROP TABLE x``.
    """
    try:
        statements = [s for s in sqlglot.parse(sql, read="sqlite") if s is not None]
    except Exception:
        return False

    if len(statements) != 1:
        return False

    stmt = statements[0]
    if not isinstance(stmt, (exp.Select, exp.Union)):
        return False

    return not any(stmt.find(kind) for kind in _FORBIDDEN)


def find_schema_problems(sql: str, schema: Dict[str, Dict[str, str]]) -> List[str]:
    """Return human-readable problems if the query references unknown tables/columns.

    An empty list means "no detectable problem" - which includes the case
    where the query couldn't be parsed/qualified (we don't block on our own
    inability to analyze it). A non-empty list is suitable for feeding back to
    the LLM as a repair hint.
    """
    try:
        expression = sqlglot.parse_one(sql, read="sqlite")
    except Exception:
        return []
    if expression is None:
        return []

    try:
        # Qualify a copy so we only validate here; callers decide what to run.
        qualify(expression.copy(), schema=schema, dialect="sqlite")
    except OptimizeError as e:
        return [str(e)]
    except Exception:
        # Any other analyzer hiccup shouldn't block an otherwise-valid query.
        return []
    return []


def enforce_limit(sql: str, limit: int = DEFAULT_ROW_LIMIT) -> str:
    """Append a LIMIT to a top-level SELECT that lacks one.

    Protects the caller (especially the web UI) from a "show me everything"
    query returning an unbounded result set. Applied at the AST level so an
    existing LIMIT or a subquery's LIMIT is never clobbered. UNION queries are
    left untouched to stay safe.
    """
    try:
        expression = sqlglot.parse_one(sql, read="sqlite")
    except Exception:
        return sql
    if isinstance(expression, exp.Select) and not expression.args.get("limit"):
        return expression.limit(limit).sql(dialect="sqlite")
    return sql

# Made with Bob
