"""Tests for the web UI's query pipeline wiring.

Covers `/api/query` end to end against `fastapi.testclient`, the way
`tests/test_cli.py` stubs out Ollama for the CLI: the interesting behaviour
here is whether the per-question schema pruning and example selection
(BACKLOG.md #1, #2) actually reach `translate_to_sql` and `guard_and_repair`,
not whether a real model produces good SQL.
"""

import json

import pytest
from fastapi.testclient import TestClient

from sql_browser.llm import LLMQueryTranslator
from sql_browser.web import create_app


@pytest.fixture(autouse=True)
def no_ollama(monkeypatch):
    """Stop the translator from requiring a live Ollama during these tests."""
    monkeypatch.setattr(LLMQueryTranslator, "_verify_ollama", lambda self: None)


def _stub_translate(monkeypatch, sql="SELECT COUNT(*) FROM orders"):
    """Replace translate_to_sql and capture how it was called."""
    captured = {}

    def fake(self, natural_query, schema_info, conversation_history=None, examples=None):
        captured["natural_query"] = natural_query
        captured["schema_info"] = schema_info
        captured["examples"] = examples
        return {"sql": sql, "explanation": "stubbed", "confidence": "high"}

    monkeypatch.setattr(LLMQueryTranslator, "translate_to_sql", fake)
    return captured


def test_query_passes_the_pruned_schema_and_matching_examples(db_path, monkeypatch):
    sidecar = db_path.with_name("shop.meta.yaml")
    sidecar.write_text(
        "version: 1\n"
        "examples:\n"
        "  - question: how many orders are there\n"
        "    sql: SELECT COUNT(*) FROM orders\n"
    )
    captured = _stub_translate(monkeypatch)

    app = create_app(str(db_path), profile=False)
    client = TestClient(app)

    response = client.post("/api/query", json={"query": "how many orders are there"})

    assert response.status_code == 200
    body = response.json()
    assert "error" not in body
    assert body["result"]["row_count"] == 1
    assert "Table: orders" in captured["schema_info"]
    assert captured["examples"] == [
        {"question": "how many orders are there", "sql": "SELECT COUNT(*) FROM orders"}
    ]


def test_query_with_no_metadata_passes_no_examples(db_path, monkeypatch):
    captured = _stub_translate(monkeypatch)

    app = create_app(str(db_path), profile=False, use_metadata=False)
    client = TestClient(app)

    response = client.post("/api/query", json={"query": "how many orders are there"})

    assert response.status_code == 200
    assert captured["examples"] == []


def test_repair_reuses_the_same_pruned_schema_as_generation(db_path, monkeypatch):
    """guard_and_repair must be handed the query's schema_info, not a stale one."""
    captured = {}

    def fake_translate(self, natural_query, schema_info, conversation_history=None, examples=None):
        return {"sql": "SELECT nonexistent_column FROM orders", "explanation": "x", "confidence": "high"}

    def fake_repair(self, natural_query, schema_info, failed_sql, error, conversation_history=None):
        captured["schema_info"] = schema_info
        return {"sql": "SELECT COUNT(*) FROM orders", "explanation": "fixed"}

    monkeypatch.setattr(LLMQueryTranslator, "translate_to_sql", fake_translate)
    monkeypatch.setattr(LLMQueryTranslator, "repair_sql", fake_repair)

    app = create_app(str(db_path), profile=False)
    client = TestClient(app)

    response = client.post("/api/query", json={"query": "how many orders are there"})

    assert response.status_code == 200
    assert "Table: orders" in captured["schema_info"]
    assert response.json()["sql"] == "SELECT COUNT(*) FROM orders"

# Made with Bob
