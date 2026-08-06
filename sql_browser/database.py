"""Database connection and schema inspection utilities."""

import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


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
        
        Returns:
            Formatted string describing the database structure
        """
        schema = self.get_full_schema()
        
        lines = ["Database Schema:\n"]
        
        for table_name, table_info in schema.items():
            lines.append(f"\nTable: {table_name} ({table_info['row_count']} rows)")
            lines.append("Columns:")
            
            column_profiles = self.profile.get(table_name, {})
            for col in table_info['columns']:
                pk_marker = " [PRIMARY KEY]" if col['pk'] else ""
                null_marker = " NOT NULL" if col['notnull'] else ""
                hint = self._format_profile_hint(column_profiles.get(col['name']))
                lines.append(f"  - {col['name']}: {col['type']}{pk_marker}{null_marker}{hint}")

        return "\n".join(lines)

    # Cap how many enum values are inlined into the LLM-facing schema text so a
    # database with many categorical columns doesn't blow up the prompt size
    # (the full list is still cached in `self.profile` for query correction).
    MAX_ENUM_VALUES_IN_SCHEMA = 8

    @staticmethod
    def _format_profile_hint(column_profile: Optional[Dict[str, Any]]) -> str:
        """Format a profiled column's known values/range as a schema hint."""
        if not column_profile:
            return ""

        if column_profile["type"] == "enum":
            values = column_profile["values"]
            shown = values[:DatabaseInspector.MAX_ENUM_VALUES_IN_SCHEMA]
            formatted = ", ".join(repr(v) for v in shown)
            if len(values) > len(shown):
                formatted += f", ... ({len(values) - len(shown)} more)"
            return f" (values: {formatted})"

        if column_profile["type"] == "range":
            return f" (range: {column_profile['min']} to {column_profile['max']})"

        return ""

# Made with Bob
