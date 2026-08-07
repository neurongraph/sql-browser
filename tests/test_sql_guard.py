"""Tests for the deterministic SQL guard, focused on the compound-query repair.

The repair is verified against a live SQLite connection rather than by string
comparison alone: the whole point is that the original query is a *syntax error*
in SQLite and the repaired one executes, so executability is the assertion that
matters.
"""

import sqlite3

import pytest

from sql_browser.sql_guard import (
    is_read_only,
    repair_compound_order_limit,
    transpile_to_sqlite,
)


@pytest.fixture
def conn(db_path):
    """A raw connection to the fixture database for executability checks."""
    connection = sqlite3.connect(str(db_path))
    yield connection
    connection.close()


def _raises_compound_error(connection, sql):
    """True if SQLite rejects `sql` with the per-branch ORDER BY/LIMIT error."""
    try:
        connection.execute(sql).fetchall()
        return False
    except sqlite3.OperationalError as e:
        return "ORDER BY" in str(e)


def test_bug_report_three_branch_union(conn):
    """The reported query: a top row per branch, one branch per insight.

    SQLite rejects the original; the repair must both fix it and preserve the
    intent of one row *per branch* (three rows), not collapse to a single row.
    """
    original = (
        "SELECT 'hi' AS label, status, MAX(total_cents) AS v FROM orders "
        "GROUP BY status ORDER BY v DESC LIMIT 1 "
        "UNION ALL "
        "SELECT 'lo' AS label, status, MIN(total_cents) AS v FROM orders "
        "GROUP BY status ORDER BY v ASC LIMIT 1 "
        "UNION ALL "
        "SELECT 'n' AS label, status, COUNT(*) AS v FROM orders "
        "GROUP BY status ORDER BY v DESC LIMIT 1"
    )
    assert _raises_compound_error(conn, original)

    fixed = repair_compound_order_limit(original)
    rows = conn.execute(fixed).fetchall()
    assert len(rows) == 3


def test_aggregate_order_limit_hoisted_to_compound(conn):
    """A trailing ORDER BY over an aggregate collapses the union to one row.

    The query is valid SQLite, so it silently returns a single row instead of
    the intended row-per-insight. The aggregate in the trailing ORDER BY marks
    it as belonging to the final grouped branch, so the repair scopes it there
    and the other insight rows survive.
    """
    original = (
        "SELECT 'Total Spending' AS Insight, SUM(total_cents) AS Value FROM orders "
        "UNION ALL "
        "SELECT 'Order Count' AS Insight, COUNT(*) AS Value FROM orders "
        "UNION ALL "
        "SELECT 'Top Status' AS Insight, status AS Value FROM orders "
        "GROUP BY status ORDER BY SUM(total_cents) DESC LIMIT 1"
    )
    assert len(conn.execute(original).fetchall()) == 1  # the bug: collapses

    fixed = repair_compound_order_limit(original)
    assert len(conn.execute(fixed).fetchall()) == 3


def test_top_n_of_union_by_output_column_untouched(conn):
    """A genuine top-N over the merged union orders by an output column.

    No aggregate, no per-branch clause -- the LIMIT is meant for the whole
    result, so the query must be returned unchanged.
    """
    original = (
        "SELECT status AS s, total_cents AS v FROM orders "
        "UNION ALL "
        "SELECT region AS s, id AS v FROM customers "
        "ORDER BY v DESC LIMIT 3"
    )
    assert repair_compound_order_limit(original) == original
    assert len(conn.execute(original).fetchall()) == 3


def test_two_branch_union_each_limited(conn):
    """Each branch keeps its own ordering/limit once wrapped in a subquery."""
    original = (
        "SELECT status, total_cents FROM orders ORDER BY total_cents DESC LIMIT 1 "
        "UNION ALL "
        "SELECT status, total_cents FROM orders ORDER BY total_cents ASC LIMIT 1"
    )
    assert _raises_compound_error(conn, original)

    fixed = repair_compound_order_limit(original)
    rows = conn.execute(fixed).fetchall()
    # Highest and lowest order totals from the fixture.
    assert sorted(r[1] for r in rows) == [700, 2500]


def test_repaired_compound_is_still_read_only(conn):
    """Wrapping branches in subqueries must not introduce a forbidden node."""
    original = (
        "SELECT status, MAX(total_cents) v FROM orders GROUP BY status "
        "ORDER BY v DESC LIMIT 1 "
        "UNION ALL "
        "SELECT status, MIN(total_cents) v FROM orders GROUP BY status "
        "ORDER BY v ASC LIMIT 1"
    )
    assert is_read_only(repair_compound_order_limit(original))


def test_legitimate_top_level_order_limit_untouched(conn):
    """A plain UNION whose trailing ORDER BY/LIMIT is genuinely top-level.

    There is no per-branch clause here, so nothing is illegal and the query
    must be returned byte-for-byte unchanged.
    """
    original = (
        "SELECT status FROM orders "
        "UNION ALL "
        "SELECT region FROM customers ORDER BY 1 LIMIT 5"
    )
    assert repair_compound_order_limit(original) == original
    conn.execute(original).fetchall()  # and it executes


def test_plain_select_untouched():
    """A non-compound query is not a set operation and is returned unchanged."""
    original = "SELECT status FROM orders ORDER BY total_cents DESC LIMIT 3"
    assert repair_compound_order_limit(original) == original


def test_unparseable_input_returned_unchanged():
    """Graceful degradation: a parse failure returns the input verbatim."""
    garbage = "this is not sql (((("
    assert repair_compound_order_limit(garbage) == garbage


def test_transpile_applies_the_repair(conn):
    """The repair is reachable through the normal normalization entry point."""
    original = (
        "SELECT status, MAX(total_cents) v FROM orders GROUP BY status "
        "ORDER BY v DESC LIMIT 1 "
        "UNION ALL "
        "SELECT status, MIN(total_cents) v FROM orders GROUP BY status "
        "ORDER BY v ASC LIMIT 1"
    )
    normalized = transpile_to_sqlite(original)
    assert normalized != original
    conn.execute(normalized).fetchall()

# Made with Bob
