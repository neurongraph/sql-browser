"""Tests for DuckDB inspection and querying."""

from pathlib import Path
import duckdb
import pytest

from sql_browser.database import DuckDBInspector, create_inspector
from sql_browser.profiler import profile_database
from sql_browser.sql_guard import enforce_limit, is_read_only, transpile_to_dialect


@pytest.fixture
def sample_duckdb(tmp_path: Path) -> Path:
    """Create a temporary DuckDB database populated with test tables."""
    db_file = tmp_path / "test.duckdb"
    con = duckdb.connect(str(db_file))
    try:
        con.execute("""
            CREATE TABLE customers (
                id INTEGER PRIMARY KEY,
                name VARCHAR NOT NULL,
                tier VARCHAR DEFAULT 'standard',
                age INTEGER
            )
        """)
        con.execute("""
            CREATE TABLE orders (
                order_id INTEGER PRIMARY KEY,
                customer_id INTEGER REFERENCES customers(id),
                status VARCHAR NOT NULL,
                total DOUBLE
            )
        """)
        con.execute("""
            INSERT INTO customers VALUES
            (1, 'Alice', 'gold', 30),
            (2, 'Bob', 'silver', 45),
            (3, 'Charlie', 'gold', 25)
        """)
        con.execute("""
            INSERT INTO orders VALUES
            (101, 1, 'completed', 150.50),
            (102, 1, 'pending', 75.00),
            (103, 2, 'completed', 200.00)
        """)
    finally:
        con.close()
    return db_file


def test_duckdb_inspector_tables(sample_duckdb: Path):
    inspector = DuckDBInspector(sample_duckdb)
    with inspector:
        tables = inspector.get_tables()
        assert "customers" in tables
        assert "orders" in tables
        assert "_sql_browser_profile" not in tables


def test_duckdb_inspector_schema(sample_duckdb: Path):
    inspector = DuckDBInspector(sample_duckdb)
    with inspector:
        schema = inspector.get_table_schema("customers")
        col_map = {c["name"]: c for c in schema}

        assert "id" in col_map
        assert col_map["id"]["pk"] is True
        assert col_map["id"]["notnull"] is True

        assert "name" in col_map
        assert col_map["name"]["pk"] is False
        assert col_map["name"]["notnull"] is True

        assert "tier" in col_map
        assert col_map["tier"]["notnull"] is False


def test_duckdb_inspector_foreign_keys(sample_duckdb: Path):
    inspector = DuckDBInspector(sample_duckdb)
    with inspector:
        fks = inspector.get_foreign_keys("orders")
        assert len(fks) == 1
        fk = fks[0]
        assert fk["table"] == "customers"
        assert fk["columns"] == [("customer_id", "id")]


def test_duckdb_inspector_sample_and_distinct(sample_duckdb: Path):
    inspector = DuckDBInspector(sample_duckdb)
    with inspector:
        sample = inspector.get_table_sample("customers", limit=2)
        assert len(sample) == 2
        assert "name" in sample[0]

        distinct_tiers = set(inspector.get_distinct_values("customers", "tier"))
        assert distinct_tiers == {"gold", "silver"}

        row_count = inspector._get_row_count("customers")
        assert row_count == 3


def test_duckdb_execute_query(sample_duckdb: Path):
    inspector = DuckDBInspector(sample_duckdb)
    with inspector:
        cols, rows = inspector.execute_query("SELECT name, age FROM customers ORDER BY id")
        assert cols == ["name", "age"]
        assert len(rows) == 3
        assert rows[0] == ("Alice", 30)


def test_duckdb_create_inspector_factory(sample_duckdb: Path):
    inspector = create_inspector(sample_duckdb)
    assert isinstance(inspector, DuckDBInspector)
    assert inspector.dialect == "duckdb"


def test_duckdb_profiling(sample_duckdb: Path):
    inspector = DuckDBInspector(sample_duckdb)
    with inspector:
        profile = profile_database(inspector)
        assert "customers" in profile
        cust_profile = profile["customers"]
        # tier has low cardinality -> enum
        assert "tier" in cust_profile
        assert cust_profile["tier"]["type"] == "enum"
        assert set(cust_profile["tier"]["values"]) == {"gold", "silver"}
        # age is numeric -> range
        assert "age" in cust_profile
        assert cust_profile["age"]["type"] == "range"
        assert cust_profile["age"]["min"] == 25
        assert cust_profile["age"]["max"] == 45


def test_duckdb_parquet_support(tmp_path: Path):
    parquet_file = tmp_path / "items.parquet"
    con = duckdb.connect()
    try:
        con.execute(f"COPY (SELECT 1 AS item_id, 'Widget' AS item_name) TO '{parquet_file}' (FORMAT PARQUET)")
    finally:
        con.close()

    inspector = create_inspector(parquet_file)
    assert isinstance(inspector, DuckDBInspector)
    with inspector:
        tables = inspector.get_tables()
        assert "items" in tables
        schema = inspector.get_table_schema("items")
        assert any(c["name"] == "item_id" for c in schema)
        cols, rows = inspector.execute_query("SELECT item_name FROM items")
        assert rows == [("Widget",)]


def test_duckdb_sql_guard():
    sql = "SELECT c.name, COUNT(o.order_id) FROM customers c JOIN orders o ON c.id = o.customer_id GROUP BY c.name"
    assert is_read_only(sql, dialect="duckdb") is True
    assert is_read_only("DELETE FROM customers", dialect="duckdb") is False

    transpiled = transpile_to_dialect(sql, dialect="duckdb")
    assert "customers" in transpiled

    limited = enforce_limit(sql, limit=50, dialect="duckdb")
    assert "LIMIT 50" in limited


# Made with Bob
