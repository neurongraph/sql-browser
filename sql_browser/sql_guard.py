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


def transpile_to_dialect(sql: str, dialect: str = "sqlite") -> str:
    """Normalize a query to target dialect syntax (sqlite or duckdb).

    Tries to parse as target dialect first, falling back to permissive dialects
    (e.g. MySQL) or unconstrained. When dialect is sqlite, runs
    repair_compound_order_limit. On total failure returns the original query.
    """
    read_dialects = (dialect, "sqlite", "mysql", None) if dialect != "sqlite" else ("sqlite", "mysql", None)
    for read in read_dialects:
        try:
            out = sqlglot.transpile(sql, read=read, write=dialect)
            if out:
                result = out[0]
                if dialect == "sqlite":
                    return repair_compound_order_limit(result)
                return result
        except Exception:
            continue
    return sql


def transpile_to_sqlite(sql: str) -> str:
    """Normalize a query to SQLite syntax.

    Tries to parse as SQLite first so a valid SQLite query is never altered.
    Only if that fails do we reinterpret under a more permissive dialect
    (MySQL) to rescue cross-dialect idioms. On total failure the original
    string is returned unchanged.

    A `repair_compound_order_limit` pass runs on the result to fix a common
    LLM mistake that even a successful transpile leaves in place.
    """
    return transpile_to_dialect(sql, dialect="sqlite")


def repair_compound_order_limit(sql: str) -> str:
    """Scope a per-branch ORDER BY / LIMIT inside a compound (UNION) query.

    SQLite binds a trailing ``ORDER BY`` / ``LIMIT`` to the *whole* compound,
    so local models produce two related mistakes when they mean a top row *per
    branch*:

    1. ``SELECT ... ORDER BY x LIMIT 1 UNION ALL SELECT ...`` -- a **syntax
       error**, because an operand may not carry its own ORDER BY/LIMIT. The
       parser leaves the earlier branches' clauses on their own SELECTs.
    2. ``... UNION ALL SELECT c, ... GROUP BY c ORDER BY SUM(x) DESC LIMIT 1``
       -- **valid but wrong**: the LIMIT silently truncates the whole union to
       one row instead of picking the top row of the final grouped branch. The
       parser hoists this trailing clause onto the compound node.

    The fix wraps each offending operand in ``SELECT * FROM (...)`` so its
    ordering/limit is scoped to a subquery, which SQLite accepts, and pushes a
    hoisted trailing clause back onto the final branch first so it is limited
    the same way. Mistake 2 is recognised only when the compound-level ORDER BY
    contains an aggregate: such an expression is meaningless applied to a merged
    union result, so it can only have been meant for the grouped branch. A plain
    compound with a genuine top-level ``ORDER BY col LIMIT n`` (top-N of the
    union) has neither an offending operand nor an aggregate and is returned
    untouched. Any parse failure returns the input unchanged.
    """
    try:
        expression = sqlglot.parse_one(sql, read="sqlite")
    except Exception:
        return sql
    if not isinstance(expression, exp.SetOperation):
        return sql

    def operand_selects(node: exp.SetOperation):
        # Yield the SELECT operands of a (possibly chained) set operation,
        # left to right, so the last yielded is the rightmost branch.
        for child in (node.this, node.expression):
            if isinstance(child, exp.SetOperation):
                yield from operand_selects(child)
            elif isinstance(child, exp.Select):
                yield child

    branch_selects = list(operand_selects(expression))
    has_branch_clause = any(
        s.args.get("order") or s.args.get("limit") for s in branch_selects
    )
    compound_order = expression.args.get("order")
    order_aggregates = compound_order is not None and compound_order.find(exp.AggFunc)
    if not (has_branch_clause or order_aggregates):
        return sql

    # Per-branch idiom confirmed: reclaim the trailing clause the parser bound
    # to the whole compound and give it back to the final branch.
    final = branch_selects[-1]
    for clause in ("order", "limit"):
        value = expression.args.get(clause)
        if value is not None and final.args.get(clause) is None:
            final.set(clause, value)
            expression.set(clause, None)

    to_wrap = [
        s for s in operand_selects(expression)
        if s.args.get("order") or s.args.get("limit")
    ]
    for select in to_wrap:
        select.replace(exp.select("*").from_(select.copy().subquery()))

    return expression.sql(dialect="sqlite")


def is_read_only(sql: str, dialect: str = "sqlite") -> bool:
    """Return True only for a single read-only SELECT/UNION statement.

    Correct where a keyword blacklist is not: it won't false-reject a column
    named ``created_at`` or ``updated_at``, and it rejects stacked statements
    like ``SELECT ...; DROP TABLE x``.
    """
    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    except Exception:
        return False

    if len(statements) != 1:
        return False

    stmt = statements[0]
    if not isinstance(stmt, (exp.Select, exp.Union)):
        return False

    return not any(stmt.find(kind) for kind in _FORBIDDEN)


def find_schema_problems(
    sql: str, schema: Dict[str, Dict[str, str]], dialect: str = "sqlite"
) -> List[str]:
    """Return human-readable problems if the query references unknown tables/columns.

    An empty list means "no detectable problem" - which includes the case
    where the query couldn't be parsed/qualified (we don't block on our own
    inability to analyze it). A non-empty list is suitable for feeding back to
    the LLM as a repair hint.
    """
    try:
        expression = sqlglot.parse_one(sql, read=dialect)
    except Exception:
        return []
    if expression is None:
        return []

    try:
        # Qualify a copy so we only validate here; callers decide what to run.
        qualify(expression.copy(), schema=schema, dialect=dialect)
    except OptimizeError as e:
        return [str(e)]
    except Exception:
        # Any other analyzer hiccup shouldn't block an otherwise-valid query.
        return []
    return []


def enforce_limit(sql: str, limit: int = DEFAULT_ROW_LIMIT, dialect: str = "sqlite") -> str:
    """Append a LIMIT to a top-level SELECT that lacks one.

    Protects the caller (especially the web UI) from a "show me everything"
    query returning an unbounded result set. Applied at the AST level so an
    existing LIMIT or a subquery's LIMIT is never clobbered. UNION queries are
    left untouched to stay safe.
    """
    try:
        expression = sqlglot.parse_one(sql, read=dialect)
    except Exception:
        return sql
    if isinstance(expression, exp.Select) and not expression.args.get("limit"):
        return expression.limit(limit).sql(dialect=dialect)
    return sql

# Made with Bob
