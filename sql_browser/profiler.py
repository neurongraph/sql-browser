"""Profiles database columns to help the LLM generate more accurate SQL.

For each column we record either:
  - an "enum" profile: the full list of distinct values, for low-cardinality
    columns (e.g. status fields) where the LLM is prone to guessing a
    plausible-but-wrong literal, or
  - a "range" profile: the min/max, for numeric columns, so the LLM knows
    what values are actually in bounds.

Results are cached in a sidecar table (`_sql_browser_profile`) inside the
database when writable, or in the inspector instance when read-only (e.g. DuckDB).
For tables with a cache table, cache is keyed by each table's current row count
so a changed table gets re-profiled while an unchanged one is loaded from cache.
"""

import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .database import DatabaseInspector

PROFILE_TABLE = "_sql_browser_profile"
ENUM_DISTINCT_THRESHOLD = 50

_META_COLUMN = "__row_count__"


def _is_numeric_type(col_type: str) -> bool:
    t = (col_type or "").upper()
    return any(key in t for key in ("INT", "REAL", "FLOA", "DOUB", "NUM", "DEC"))


def _ensure_profile_table(connection: Any) -> bool:
    try:
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {PROFILE_TABLE} (
                table_name TEXT NOT NULL,
                column_name TEXT NOT NULL,
                profile_type TEXT NOT NULL,
                profile_json TEXT NOT NULL,
                computed_at TEXT NOT NULL,
                PRIMARY KEY (table_name, column_name)
            )
            """
        )
        return True
    except Exception:
        return False


def _load_cached_table_profile(
    connection: Any, table: str, current_row_count: int
) -> Optional[Dict[str, Dict[str, Any]]]:
    """Return the cached profile for a table if it's still fresh, else None."""
    try:
        cursor = connection.cursor()
        cursor.execute(
            f"SELECT column_name, profile_type, profile_json FROM {PROFILE_TABLE} WHERE table_name = ?",
            (table,),
        )
        rows = cursor.fetchall()
        if not rows:
            return None

        table_profile: Dict[str, Dict[str, Any]] = {}
        cached_row_count = None
        for column_name, profile_type, profile_json in rows:
            if column_name == _META_COLUMN:
                cached_row_count = json.loads(profile_json)["row_count"]
                continue
            table_profile[column_name] = {"type": profile_type, **json.loads(profile_json)}

        if cached_row_count != current_row_count:
            return None
        return table_profile
    except Exception:
        return None


def _profile_column(
    connection: Any, table: str, column: str, col_type: str
) -> Optional[Dict[str, Any]]:
    cursor = connection.cursor()

    if _is_numeric_type(col_type):
        cursor.execute(f'SELECT MIN("{column}"), MAX("{column}") FROM "{table}"')
        min_v, max_v = cursor.fetchone()
        if min_v is None and max_v is None:
            return None
        return {"type": "range", "min": min_v, "max": max_v}

    cursor.execute(f'SELECT COUNT(DISTINCT "{column}") FROM "{table}"')
    distinct_count = cursor.fetchone()[0]
    if distinct_count == 0 or distinct_count > ENUM_DISTINCT_THRESHOLD:
        return None

    cursor.execute(
        f'SELECT DISTINCT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL '
        f'LIMIT ?',
        (ENUM_DISTINCT_THRESHOLD,),
    )
    values = [row[0] for row in cursor.fetchall()]
    return {"type": "enum", "values": values}


def _save_table_profile(
    connection: Any,
    table: str,
    row_count: int,
    table_profile: Dict[str, Dict[str, Any]],
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    connection.execute(
        f"DELETE FROM {PROFILE_TABLE} WHERE table_name = ?", (table,)
    )
    rows = [
        (table, _META_COLUMN, "meta", json.dumps({"row_count": row_count}), now)
    ]
    for column, info in table_profile.items():
        info_without_type = {k: v for k, v in info.items() if k != "type"}
        rows.append((table, column, info["type"], json.dumps(info_without_type), now))
    connection.executemany(
        f"INSERT INTO {PROFILE_TABLE} (table_name, column_name, profile_type, profile_json, computed_at) "
        f"VALUES (?, ?, ?, ?, ?)",
        rows,
    )


def profile_database(
    db_inspector: DatabaseInspector, force: bool = False
) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """Profile every table/column, reusing the cached profile where still fresh.

    Args:
        db_inspector: Connected database inspector
        force: Re-profile every table even if a fresh cache entry exists

    Returns:
        Dict mapping table name -> column name -> profile info
        (e.g. {"type": "enum", "values": [...]} or {"type": "range", "min": ..., "max": ...})
    """
    connection = db_inspector.connection
    if not connection:
        raise RuntimeError("Database not connected")

    if not force and db_inspector.profile:
        return db_inspector.profile

    can_cache_in_db = _ensure_profile_table(connection)

    full_profile: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for table in db_inspector.get_tables():
        row_count = db_inspector._get_row_count(table)

        if not force and can_cache_in_db:
            cached = _load_cached_table_profile(connection, table, row_count)
            if cached is not None:
                full_profile[table] = cached
                continue

        table_profile: Dict[str, Dict[str, Any]] = {}
        for col in db_inspector.get_table_schema(table):
            result = _profile_column(connection, table, col["name"], col["type"])
            if result is not None:
                table_profile[col["name"]] = result

        if can_cache_in_db:
            try:
                _save_table_profile(connection, table, row_count, table_profile)
            except Exception:
                can_cache_in_db = False

        full_profile[table] = table_profile

    if can_cache_in_db:
        try:
            connection.commit()
        except Exception:
            pass

    db_inspector.profile = full_profile
    return full_profile


# Made with Bob
