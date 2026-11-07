"""Tests for the non-interactive CLI query and metadata subcommands."""

import json
from pathlib import Path
import duckdb
import pytest
from typer.testing import CliRunner

from sql_browser.cli import app
from sql_browser.llm import LLMQueryTranslator

runner = CliRunner()


@pytest.fixture(autouse=True)
def no_ollama(monkeypatch):
    """Stop the translator from requiring a live Ollama during tests."""
    monkeypatch.setattr(
        "sql_browser.llm.LLMQueryTranslator._verify_ollama", lambda self: None
    )


@pytest.fixture
def duckdb_db(tmp_path: Path) -> Path:
    """Create a temporary DuckDB database populated with test tables."""
    db_file = tmp_path / "shop.duckdb"
    con = duckdb.connect(str(db_file))
    try:
        con.execute("""
            CREATE TABLE products (
                id INTEGER PRIMARY KEY,
                title VARCHAR NOT NULL,
                category VARCHAR,
                price DOUBLE
            )
        """)
        con.execute("""
            INSERT INTO products VALUES
            (1, 'Laptop', 'electronics', 999.99),
            (2, 'Phone', 'electronics', 499.50),
            (3, 'Desk', 'furniture', 150.00)
        """)
    finally:
        con.close()
    return db_file


def _stub_translate(monkeypatch, sql: str, explanation: str = "Test explanation", confidence: str = "high"):
    def fake_translate(self, natural_language_query, schema_info, conversation_history, examples=None):
        return {
            "sql": sql,
            "explanation": explanation,
            "confidence": confidence,
        }
    monkeypatch.setattr("sql_browser.llm.LLMQueryTranslator.translate_to_sql", fake_translate)


def test_cli_query_dry_run_json(duckdb_db: Path, monkeypatch):
    _stub_translate(monkeypatch, "SELECT * FROM products")

    result = runner.invoke(
        app,
        ["query", "show all products", str(duckdb_db), "--json", "--dry-run"],
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["question"] == "show all products"
    assert "products" in data["sql"]
    assert data["rows"] == []
    assert data["notes"] == "Test explanation"
    assert "warnings" in data
    assert "corrections" in data


def test_cli_query_execution_json(duckdb_db: Path, monkeypatch):
    _stub_translate(monkeypatch, "SELECT title, price FROM products ORDER BY id")

    result = runner.invoke(
        app,
        ["query", "list products and prices", str(duckdb_db), "--json"],
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["question"] == "list products and prices"
    assert len(data["rows"]) == 3
    assert data["rows"][0]["title"] == "Laptop"
    assert data["rows"][0]["price"] == 999.99


def test_cli_query_human_readable_output(duckdb_db: Path, monkeypatch):
    _stub_translate(monkeypatch, "SELECT title FROM products")

    result = runner.invoke(
        app,
        ["query", "show titles", str(duckdb_db)],
    )

    assert result.exit_code == 0
    assert "Question:" in result.output
    assert "SQL:" in result.output
    assert "Laptop" in result.output


def test_cli_query_rejects_non_readonly(duckdb_db: Path, monkeypatch):
    _stub_translate(monkeypatch, "DELETE FROM products")

    result = runner.invoke(
        app,
        ["query", "delete all products", str(duckdb_db), "--json"],
    )

    assert result.exit_code != 0
    data = json.loads(result.output)
    assert "error" in data
    assert "read-only" in data["error"].lower()


def test_cli_metadata_subcommands(duckdb_db: Path, tmp_path: Path):
    meta_file = tmp_path / "custom.meta.yaml"

    # 1. metadata init
    init_res = runner.invoke(
        app,
        ["metadata", "init", str(duckdb_db), "--out", str(meta_file)],
    )
    assert init_res.exit_code == 0
    assert meta_file.exists()
    assert "products:" in meta_file.read_text()

    # 2. metadata validate
    val_res = runner.invoke(
        app,
        ["metadata", "validate", str(duckdb_db), "--metadata", str(meta_file)],
    )
    assert val_res.exit_code == 0
    assert "Metadata is valid" in val_res.output or "0 warnings" in val_res.output


def test_cli_metadata_regenerate(duckdb_db: Path, tmp_path: Path, monkeypatch):
    meta_file = tmp_path / "prose.meta.yaml"
    meta_file.write_text(
        "version: 1\n"
        "prose:\n"
        "  tables:\n"
        "    products:\n"
        "      price is the catalog price.\n"
    )

    def fake_parse(self, section, prose_text, schema_hint, table=None):
        return {
            "description": "products",
            "columns": {"price": {"description": "catalog price"}},
        }

    monkeypatch.setattr(
        "sql_browser.llm.LLMQueryTranslator.parse_prose_to_metadata", fake_parse
    )

    regen_res = runner.invoke(
        app,
        ["metadata", "regenerate", str(duckdb_db), "--metadata", str(meta_file)],
    )
    assert regen_res.exit_code == 0
    assert "catalog price" in meta_file.read_text()


# Made with Bob
