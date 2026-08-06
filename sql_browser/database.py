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

    def get_foreign_keys(self, table_name: str) -> List[Dict[str, str]]:
        """Get the foreign keys declared on a table.

        These are the join paths the database already knows about, and reading
        them costs nothing - so a SQLite file with real constraints gets a join
        graph without anyone writing metadata by hand.

        Args:
            table_name: Name of the table

        Returns:
            List of {'from': column, 'table': parent table, 'to': parent column}.
            Empty for tables with no constraints - notably every table in a
            workbook loaded from Excel, which is why relationships can also be
            declared by hand in a metadata file.
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

        foreign_keys = []
        for row in rows:
            parent_table, from_column, to_column = row[2], row[3], row[4]
            # `to` is NULL when the constraint references the parent's primary
            # key implicitly (`REFERENCES customers` with no column).
            if to_column is None:
                to_column = self._primary_key_column(parent_table)
                if to_column is None:
                    continue
            if not from_column:
                continue
            foreign_keys.append(
                {"from": from_column, "table": parent_table, "to": to_column}
            )
        return foreign_keys

    def _primary_key_column(self, table_name: str) -> Optional[str]:
        """Return a table's single-column primary key, or None if there isn't one."""
        try:
            primary_keys = [col["name"] for col in self.get_table_schema(table_name) if col["pk"]]
        except sqlite3.Error:
            return None
        return primary_keys[0] if len(primary_keys) == 1 else None

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
