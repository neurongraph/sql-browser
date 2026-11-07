"""Database connection and schema inspection utilities."""

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, Union

from .excel_loader import EXCEL_EXTENSIONS, convert_xlsx_to_sqlite
from .logger import get_logger

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance
    from .metadata import Metadata

logger = get_logger(__name__)


class DatabaseInspector:
    """Base class for database connection and schema inspection."""

    dialect: str = "sqlite"

    def __new__(cls, db_path: Any, *args, **kwargs):
        """Factory instantiation: return dialect-specific inspector if base class called."""
        if cls is DatabaseInspector:
            path = Path(db_path)
            suffix = path.suffix.lower()
            if suffix in {".duckdb", ".parquet"}:
                target_cls = DuckDBInspector
            else:
                target_cls = SQLiteInspector
            return super().__new__(target_cls)
        return super().__new__(cls)

    def __init__(self, db_path: Union[str, Path]):
        """Initialize database inspector.

        Args:
            db_path: Path to the database file
        """
        self.db_path = Path(db_path)
        if not self.db_path.exists():
            raise FileNotFoundError(f"Database file not found: {db_path}")

        self.connection: Optional[Any] = None
        self._schema_cache: Optional[Dict[str, Any]] = None
        self.profile: Dict[str, Dict[str, Any]] = {}
        #: Curated semantic layer (see metadata.py), populated by the caller
        #: after connecting. None until then, and left None when disabled.
        self.metadata: Optional["Metadata"] = None
        #: Problems found reconciling that metadata against this schema, kept so
        #: the CLI and web UI can surface them instead of only logging them.
        self.metadata_warnings: List[str] = []
        self._temp_db_path: Optional[Path] = None

    def connect(self, check_same_thread: bool = True) -> None:
        """Establish connection to the database."""
        raise NotImplementedError

    def disconnect(self) -> None:
        """Close the database connection."""
        raise NotImplementedError

    def __enter__(self):
        """Context manager entry."""
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.disconnect()

    def get_tables(self) -> List[str]:
        """Get list of all tables in the database."""
        raise NotImplementedError

    def get_table_schema(self, table_name: str) -> List[Dict[str, Any]]:
        """Get schema information for a specific table."""
        raise NotImplementedError

    def get_foreign_keys(self, table_name: str) -> List[Dict[str, Any]]:
        """Get the foreign key constraints declared on a table."""
        raise NotImplementedError

    def get_table_sample(self, table_name: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Get sample rows from a table."""
        raise NotImplementedError

    def get_distinct_values(self, table_name: str, column_name: str, limit: int = 50) -> List[Any]:
        """Get the distinct values present in a column."""
        raise NotImplementedError

    def execute_query(self, query: str) -> Tuple[List[str], List[Tuple]]:
        """Execute a SQL query and return results."""
        raise NotImplementedError

    def _get_row_count(self, table_name: str) -> int:
        """Get the number of rows in a table."""
        raise NotImplementedError

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
                "columns": columns,
                "sample_data": sample,
                "row_count": self._get_row_count(table),
            }

        self._schema_cache = schema
        return schema

    def get_schema_description(self, question: Optional[str] = None) -> str:
        """Get a human-readable description of the database schema.

        Composing this needs the profile, the declared foreign keys and any
        curated metadata as well as the schema, so the rendering lives in
        context.py; this stays as the entry point callers already use. Imported
        lazily because that module needs this class in turn.

        Args:
            question: The user's natural-language question, if there is one.
                Passed through to `render_schema_context` to prune full detail
                to the tables it scores as relevant. None renders every table
                in full.

        Returns:
            Formatted string describing the database structure
        """
        from .context import render_schema_context

        return render_schema_context(self, question=question)


class SQLiteInspector(DatabaseInspector):
    """Handles SQLite database connections and schema inspection."""

    dialect: str = "sqlite"

    def connect(self, check_same_thread: bool = True) -> None:
        """Establish connection to the SQLite database.

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
        """Close the database connection and clean up temp files if any."""
        if self.connection:
            self.connection.close()
            self.connection = None
        if self._temp_db_path:
            self._temp_db_path.unlink(missing_ok=True)
            self._temp_db_path = None

    def get_tables(self) -> List[str]:
        """Get list of all tables in the SQLite database."""
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
        """Get schema information for a specific SQLite table."""
        if not self.connection:
            raise RuntimeError("Database not connected")

        cursor = self.connection.cursor()
        cursor.execute(f'PRAGMA table_info("{table_name}")')

        columns = []
        for row in cursor.fetchall():
            columns.append({
                "cid": row[0],
                "name": row[1],
                "type": row[2],
                "notnull": bool(row[3]),
                "default_value": row[4],
                "pk": bool(row[5]),
            })
        return columns

    def get_foreign_keys(self, table_name: str) -> List[Dict[str, Any]]:
        """Get the foreign key constraints declared on a SQLite table."""
        if not self.connection:
            raise RuntimeError("Database not connected")

        cursor = self.connection.cursor()
        try:
            cursor.execute(f'PRAGMA foreign_key_list("{table_name}")')
            rows = cursor.fetchall()
        except sqlite3.Error as e:
            logger.debug(f"Could not read foreign keys for {table_name}: {e}")
            return []

        # Group by constraint id, preserving both constraint and column order.
        constraints: Dict[Any, Dict[str, Any]] = {}
        for row in rows:
            constraint_id, seq, parent_table = row[0], row[1], row[2]
            from_column, to_column = row[3], row[4]

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
            foreign_keys.append({
                "table": entry["table"],
                "columns": [(child, parent) for _, child, parent in ordered],
            })
        return foreign_keys

    def _implicit_parent_column(self, table_name: str, seq: int) -> Optional[str]:
        """Resolve the parent column of a constraint that names no column list."""
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
        """Get sample rows from a table."""
        if not self.connection:
            raise RuntimeError("Database not connected")

        cursor = self.connection.cursor()
        cursor.execute(f'SELECT * FROM "{table_name}" LIMIT ?', (limit,))
        return [dict(row) for row in cursor.fetchall()]

    def get_distinct_values(self, table_name: str, column_name: str, limit: int = 50) -> List[Any]:
        """Get the distinct values present in a column."""
        if not self.connection:
            raise RuntimeError("Database not connected")

        cursor = self.connection.cursor()
        cursor.execute(
            f'SELECT DISTINCT "{column_name}" FROM "{table_name}" LIMIT ?', (limit,)
        )
        return [row[0] for row in cursor.fetchall()]

    def execute_query(self, query: str) -> Tuple[List[str], List[Tuple]]:
        """Execute a SQL query and return results."""
        if not self.connection:
            raise RuntimeError("Database not connected")

        cursor = self.connection.cursor()
        cursor.execute(query)

        column_names = (
            [description[0] for description in cursor.description]
            if cursor.description
            else []
        )
        rows = cursor.fetchall()
        return column_names, rows

    def _get_row_count(self, table_name: str) -> int:
        """Get the number of rows in a table."""
        if not self.connection:
            raise RuntimeError("Database not connected")

        cursor = self.connection.cursor()
        cursor.execute(f'SELECT COUNT(*) FROM "{table_name}"')
        return cursor.fetchone()[0]


class DuckDBInspector(DatabaseInspector):
    """Handles DuckDB and Parquet database connections and schema inspection."""

    dialect: str = "duckdb"

    def connect(self, check_same_thread: bool = True) -> None:
        """Establish connection to DuckDB."""
        import duckdb

        if self.db_path.suffix.lower() == ".parquet":
            self.connection = duckdb.connect()
            table_name = self.db_path.stem
            escaped_table = table_name.replace('"', '""')
            escaped_path = str(self.db_path).replace("'", "''")
            self.connection.execute(
                f'CREATE VIEW "{escaped_table}" AS SELECT * FROM read_parquet(\'{escaped_path}\')'
            )
        else:
            self.connection = duckdb.connect(str(self.db_path), read_only=True)

    def disconnect(self) -> None:
        """Close the DuckDB connection."""
        if self.connection:
            self.connection.close()
            self.connection = None

    def get_tables(self) -> List[str]:
        """Get list of all tables in the DuckDB database."""
        if not self.connection:
            raise RuntimeError("Database not connected")

        cursor = self.connection.cursor()
        cursor.execute("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'main' AND table_name != '_sql_browser_profile'
            ORDER BY table_name
        """)
        return [row[0] for row in cursor.fetchall()]

    def get_table_schema(self, table_name: str) -> List[Dict[str, Any]]:
        """Get schema information for a specific DuckDB table."""
        if not self.connection:
            raise RuntimeError("Database not connected")

        cursor = self.connection.cursor()

        pk_cols = set()
        try:
            cursor.execute("""
                SELECT constraint_column_names
                FROM duckdb_constraints()
                WHERE table_name = ? AND constraint_type = 'PRIMARY KEY'
            """, (table_name,))
            for (col_names,) in cursor.fetchall():
                if col_names:
                    pk_cols.update(col_names)
        except Exception as e:
            logger.debug(f"Could not read constraints for {table_name}: {e}")

        cursor.execute("""
            SELECT column_name, data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_name = ?
            ORDER BY ordinal_position
        """, (table_name,))

        columns = []
        for idx, row in enumerate(cursor.fetchall()):
            col_name, data_type, is_nullable, col_default = row[0], row[1], row[2], row[3]
            columns.append({
                "cid": idx,
                "name": col_name,
                "type": data_type,
                "notnull": (is_nullable == "NO"),
                "default_value": col_default,
                "pk": (col_name in pk_cols),
            })
        return columns

    def get_foreign_keys(self, table_name: str) -> List[Dict[str, Any]]:
        """Get foreign key constraints declared on a DuckDB table."""
        if not self.connection:
            raise RuntimeError("Database not connected")

        foreign_keys = []
        try:
            cursor = self.connection.cursor()
            cursor.execute("""
                SELECT referenced_table, constraint_column_names, referenced_column_names
                FROM duckdb_constraints()
                WHERE table_name = ? AND constraint_type = 'FOREIGN KEY'
            """, (table_name,))
            for ref_table, child_cols, parent_cols in cursor.fetchall():
                if ref_table and child_cols and parent_cols:
                    foreign_keys.append({
                        "table": ref_table,
                        "columns": list(zip(child_cols, parent_cols)),
                    })
        except Exception as e:
            logger.debug(f"Could not read foreign keys for {table_name}: {e}")
        return foreign_keys

    def get_table_sample(self, table_name: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Get sample rows from a DuckDB table."""
        if not self.connection:
            raise RuntimeError("Database not connected")

        cursor = self.connection.cursor()
        safe_table = table_name.replace('"', '""')
        cursor.execute(f'SELECT * FROM "{safe_table}" LIMIT ?', (limit,))
        cols = [d[0] for d in cursor.description] if cursor.description else []
        return [dict(zip(cols, row)) for row in cursor.fetchall()]

    def get_distinct_values(self, table_name: str, column_name: str, limit: int = 50) -> List[Any]:
        """Get distinct values present in a column."""
        if not self.connection:
            raise RuntimeError("Database not connected")

        cursor = self.connection.cursor()
        safe_table = table_name.replace('"', '""')
        safe_col = column_name.replace('"', '""')
        cursor.execute(f'SELECT DISTINCT "{safe_col}" FROM "{safe_table}" LIMIT ?', (limit,))
        return [row[0] for row in cursor.fetchall()]

    def execute_query(self, query: str) -> Tuple[List[str], List[Tuple]]:
        """Execute a SQL query on DuckDB and return results."""
        if not self.connection:
            raise RuntimeError("Database not connected")

        cursor = self.connection.cursor()
        cursor.execute(query)
        column_names = (
            [d[0] for d in cursor.description] if cursor.description else []
        )
        rows = cursor.fetchall()
        return column_names, rows

    def _get_row_count(self, table_name: str) -> int:
        """Get row count of a DuckDB table."""
        if not self.connection:
            raise RuntimeError("Database not connected")

        cursor = self.connection.cursor()
        safe_table = table_name.replace('"', '""')
        cursor.execute(f'SELECT COUNT(*) FROM "{safe_table}"')
        return cursor.fetchone()[0]


def create_inspector(db_path: Union[str, Path]) -> DatabaseInspector:
    """Create an appropriate DatabaseInspector for the given database file.

    Args:
        db_path: Path to database (.db, .sqlite, .duckdb, .parquet, .xlsx, .xlsm)

    Returns:
        DuckDBInspector for DuckDB/Parquet files, SQLiteInspector for SQLite/Excel.
    """
    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(f"Database file not found: {db_path}")

    suffix = path.suffix.lower()
    if suffix in {".duckdb", ".parquet"}:
        return DuckDBInspector(path)
    elif suffix in EXCEL_EXTENSIONS:
        temp_sqlite = convert_xlsx_to_sqlite(path)
        inspector = SQLiteInspector(temp_sqlite)
        inspector._temp_db_path = temp_sqlite
        return inspector
    else:
        return SQLiteInspector(path)


# Made with Bob
