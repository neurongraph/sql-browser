"""Database connection and schema inspection utilities."""

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from .logger import get_logger

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance
    from .metadata import Metadata

logger = get_logger(__name__)


class DatabaseInspector:
    """Handles SQLite database connections and schema inspection."""

    def __init__(self, db_path: str):
        """Initialize database inspector.
        
        Args:
            db_path: Path to the SQLite database file
        """
        self.db_path = Path(db_path)
        if not self.db_path.exists():
            raise FileNotFoundError(f"Database file not found: {db_path}")
        
        self.connection: Optional[sqlite3.Connection] = None
        self._schema_cache: Optional[Dict[str, Any]] = None
        self.profile: Dict[str, Dict[str, Any]] = {}
        #: Curated semantic layer (see metadata.py), populated by the caller
        #: after connecting. None until then, and left None when disabled.
        self.metadata: Optional["Metadata"] = None
        #: Problems found reconciling that metadata against this schema, kept so
        #: the CLI and web UI can surface them instead of only logging them.
        self.metadata_warnings: List[str] = []

    def connect(self, check_same_thread: bool = True) -> None:
        """Establish connection to the database.

        Args:
            check_same_thread: Passed to sqlite3.connect. The web server serves
                requests from a thread pool, so it connects with this set to
                False (and serializes access with a lock).
        """
        self.connection = sqlite3.connect(
            str(self.db_path), check_same_thread=check_same_thread
        )
        self.connection.row_factory = sqlite3.Row

    def disconnect(self) -> None:
        """Close the database connection."""
        if self.connection:
            self.connection.close()
            self.connection = None

    def __enter__(self):
        """Context manager entry."""
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.disconnect()

    def get_tables(self) -> List[str]:
        """Get list of all tables in the database.
        
        Returns:
            List of table names
        """
        if not self.connection:
            raise RuntimeError("Database not connected")
        
        cursor = self.connection.cursor()
        cursor.execute("""
            SELECT name FROM sqlite_master
            WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name != '_sql_browser_profile'
            ORDER BY name
        """)
        return [row[0] for row in cursor.fetchall()]

    def get_table_schema(self, table_name: str) -> List[Dict[str, Any]]:
        """Get schema information for a specific table.
        
        Args:
            table_name: Name of the table
            
        Returns:
            List of column information dictionaries
        """
        if not self.connection:
            raise RuntimeError("Database not connected")
        
        cursor = self.connection.cursor()
        cursor.execute(f"PRAGMA table_info({table_name})")
        
        columns = []
        for row in cursor.fetchall():
            columns.append({
                'cid': row[0],
                'name': row[1],
                'type': row[2],
                'notnull': bool(row[3]),
                'default_value': row[4],
                'pk': bool(row[5])
            })
        return columns

    def get_foreign_keys(self, table_name: str) -> List[Dict[str, Any]]:
        """Get the foreign key constraints declared on a table.

        These are the join paths the database already knows about, and reading
        them costs nothing - so a SQLite file with real constraints gets a join
        graph without anyone writing metadata by hand.

        One entry per *constraint*, not per column. ``PRAGMA foreign_key_list``
        returns one row per column, with the columns of a composite key sharing
        an ``id`` and distinguished by ``seq``; grouping them back together is
        essential, because a composite key joined on only one of its columns
        fans out and silently returns wrong rows.

        Args:
            table_name: Name of the table

        Returns:
            List of {'table': parent table, 'columns': [(child col, parent col)]}
            with the column pairs in declaration order. Empty for tables with no
            constraints - notably every table in a workbook loaded from Excel,
            which is why relationships can also be declared by hand in a
            metadata file.
        """
        if not self.connection:
            raise RuntimeError("Database not connected")

        cursor = self.connection.cursor()
        try:
            cursor.execute(f'PRAGMA foreign_key_list("{table_name}")')
            rows = cursor.fetchall()
        except sqlite3.Error as e:
            # A malformed constraint shouldn't cost us the whole join graph.
            logger.debug(f"Could not read foreign keys for {table_name}: {e}")
            return []

        # Group by constraint id, preserving both constraint and column order.
        constraints: Dict[Any, Dict[str, Any]] = {}
        for row in rows:
            constraint_id, seq, parent_table = row[0], row[1], row[2]
            from_column, to_column = row[3], row[4]

            # `to` is NULL when the constraint references the parent's primary
            # key implicitly (`REFERENCES customers` with no column list).
            if to_column is None:
                to_column = self._implicit_parent_column(parent_table, seq)
            if not from_column or not to_column:
                logger.debug(
                    f"Skipping unresolvable foreign key on {table_name} "
                    f"(constraint {constraint_id}, seq {seq})"
                )
                constraints.pop(constraint_id, None)
                continue

            entry = constraints.setdefault(
                constraint_id, {"table": parent_table, "columns": []}
            )
            entry["columns"].append((seq, from_column, to_column))

        foreign_keys = []
        for entry in constraints.values():
            ordered = sorted(entry["columns"], key=lambda c: c[0])
            foreign_keys.append(
                {
                    "table": entry["table"],
                    "columns": [(child, parent) for _, child, parent in ordered],
                }
            )
        return foreign_keys

    def _implicit_parent_column(self, table_name: str, seq: int) -> Optional[str]:
        """Resolve the parent column of a constraint that names no column list.

        `REFERENCES customers` targets the parent's primary key, so the column
        at position `seq` of that key is the one this constraint column pairs
        with. Returns None if the parent has no usable primary key, which makes
        the caller drop the constraint rather than guess at it.

        Reads `PRAGMA table_info` directly rather than going through
        get_table_schema(), because that coerces the `pk` field to a bool and
        here its value matters: for a composite primary key it's the 1-based
        position of the column within the key, which is what pairs it with `seq`.
        """
        cursor = self.connection.cursor()
        try:
            cursor.execute(f'PRAGMA table_info("{table_name}")')
            rows = cursor.fetchall()
        except sqlite3.Error:
            return None

        key_columns = sorted(
            ((row[5], row[1]) for row in rows if row[5]), key=lambda c: c[0]
        )
        return key_columns[seq][1] if seq < len(key_columns) else None

    def get_table_sample(self, table_name: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Get sample rows from a table.
        
        Args:
            table_name: Name of the table
            limit: Maximum number of rows to return
            
        Returns:
            List of row dictionaries
        """
        if not self.connection:
            raise RuntimeError("Database not connected")
        
        cursor = self.connection.cursor()
        cursor.execute(f"SELECT * FROM {table_name} LIMIT ?", (limit,))
        
        return [dict(row) for row in cursor.fetchall()]

    def get_full_schema(self) -> Dict[str, Any]:
        """Get complete schema information for all tables.
        
        Returns:
            Dictionary mapping table names to their schema information
        """
        if self._schema_cache:
            return self._schema_cache
        
        schema = {}
        tables = self.get_tables()
        
        for table in tables:
            columns = self.get_table_schema(table)
            sample = self.get_table_sample(table, limit=3)
            
            schema[table] = {
                'columns': columns,
                'sample_data': sample,
                'row_count': self._get_row_count(table)
            }
        
        self._schema_cache = schema
        return schema

    def _get_row_count(self, table_name: str) -> int:
        """Get the number of rows in a table.
        
        Args:
            table_name: Name of the table
            
        Returns:
            Number of rows
        """
        if not self.connection:
            raise RuntimeError("Database not connected")
        
        cursor = self.connection.cursor()
        cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
        return cursor.fetchone()[0]

    def get_distinct_values(self, table_name: str, column_name: str, limit: int = 50) -> List[Any]:
        """Get the distinct values present in a column.

        Args:
            table_name: Name of the table
            column_name: Name of the column
            limit: Maximum number of distinct values to return

        Returns:
            List of distinct values found in the column
        """
        if not self.connection:
            raise RuntimeError("Database not connected")

        cursor = self.connection.cursor()
        cursor.execute(
            f'SELECT DISTINCT "{column_name}" FROM "{table_name}" LIMIT ?', (limit,)
        )
        return [row[0] for row in cursor.fetchall()]

    def execute_query(self, query: str) -> Tuple[List[str], List[Tuple]]:
        """Execute a SQL query and return results.
        
        Args:
            query: SQL query to execute
            
        Returns:
            Tuple of (column_names, rows)
        """
        if not self.connection:
            raise RuntimeError("Database not connected")
        
        cursor = self.connection.cursor()
        cursor.execute(query)
        
        # Get column names
        column_names = [description[0] for description in cursor.description] if cursor.description else []
        
        # Fetch all rows
        rows = cursor.fetchall()
        
        return column_names, rows

    def get_schema_description(self) -> str:
        """Get a human-readable description of the database schema.

        Composing this needs the profile, the declared foreign keys and any
        curated metadata as well as the schema, so the rendering lives in
        context.py; this stays as the entry point callers already use. Imported
        lazily because that module needs this class in turn.

        Returns:
            Formatted string describing the database structure
        """
        from .context import render_schema_context

        return render_schema_context(self)

# Made with Bob
