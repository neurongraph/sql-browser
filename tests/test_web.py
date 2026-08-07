"""Tests for the web UI's query pipeline wiring.

Covers `/api/query` end to end against `fastapi.testclient`, the way
`tests/test_cli.py` stubs out Ollama for the CLI: the interesting behaviour
here is whether the per-question schema pruning and example selection
(BACKLOG.md #1, #2) actually reach `translate_to_sql` and `guard_and_repair`,
not whether a real model produces good SQL.
"""

import json

import openpyxl
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


# --------------------------------------------------------------------------- #
# Metadata editor endpoints (Phase A)
# --------------------------------------------------------------------------- #

def _stub_parse(monkeypatch, response=None, raises=False):
    """Stub parse_prose_to_metadata so no live model is needed."""
    def fake(self, section, prose_text, schema_hint, table=None):
        if raises:
            raise RuntimeError("ollama down")
        return response or {}

    monkeypatch.setattr(LLMQueryTranslator, "parse_prose_to_metadata", fake)


# A table stanza with one valid code (P, in the data) and one invented (Z).
ORDERS_STANZA = {
    "description": "orders",
    "columns": {
        "status": {"values": {"P": {"label": "Pending"}, "Z": {"label": "Invented"}}}
    },
}


def test_get_metadata_binds_the_raw_document_while_schema_is_reconciled(db_path, monkeypatch):
    """The invariant: /api/metadata shows the raw code, /api/schema drops it."""
    db_path.with_name("shop.meta.yaml").write_text(
        "version: 1\n"
        "tables:\n"
        "  orders:\n"
        "    columns:\n"
        "      status:\n"
        "        values:\n"
        "          P: {label: Pending}\n"
        "          Z: {label: Invented}\n"
    )
    app = create_app(str(db_path), profile=True)
    client = TestClient(app)

    meta = client.get("/api/metadata").json()
    schema = client.get("/api/schema").json()

    # RAW view knows the file carries a code the data doesn't have.
    assert meta["structured_present"] is True
    assert meta["reconcile"]["dropped"] >= 1
    assert any("'Z'" in w for w in meta["reconcile"]["warnings"])
    # Reconciled schema never surfaces the invented code.
    orders = next(t for t in schema["tables"] if t["name"] == "orders")
    status = next(c for c in orders["columns"] if c["name"] == "status")
    assert "Z" not in json.dumps(status)


def test_preview_writes_nothing(db_path, monkeypatch):
    _stub_parse(monkeypatch, ORDERS_STANZA)
    sidecar = db_path.with_name("shop.meta.yaml")
    app = create_app(str(db_path), profile=True)
    client = TestClient(app)

    response = client.post(
        "/api/metadata/preview", json={"prose": {"tables": {"orders": "status codes"}}}
    )
    body = response.json()

    assert response.status_code == 200
    assert body["dropped"] >= 1  # Z is dropped by reconcile
    assert not sidecar.exists()  # a dry run leaves no file


def test_save_writes_prose_and_reloads(db_path, monkeypatch):
    _stub_parse(monkeypatch, ORDERS_STANZA)
    sidecar = db_path.with_name("shop.meta.yaml")
    app = create_app(str(db_path), profile=True)
    client = TestClient(app)

    response = client.post(
        "/api/metadata", json={"prose": {"tables": {"orders": "P means Pending"}}}
    )
    body = response.json()

    assert body["ok"] is True
    assert body["matched"] >= 1  # the valid P code survived reconcile
    assert body["dropped"] >= 1  # the invented Z code was dropped
    assert sidecar.exists()
    on_disk = sidecar.read_text(encoding="utf-8")
    assert "prose:" in on_disk
    assert "from_prose_digest:" in on_disk
    # write_document persists the RAW structured block (reconcile is a read-time
    # gate), so the generated label is on disk; the offer is now consumed.
    assert "Pending" in on_disk
    assert client.get("/api/config").json()["metadata_suggestion"] is False


def test_save_degrades_when_the_model_is_down(db_path, monkeypatch):
    """Ollama down: prose is still written, warnings returned, no 500."""
    _stub_parse(monkeypatch, raises=True)
    sidecar = db_path.with_name("shop.meta.yaml")
    app = create_app(str(db_path), profile=True)
    client = TestClient(app)

    response = client.post(
        "/api/metadata", json={"prose": {"database": "Some notes."}}
    )
    body = response.json()

    assert response.status_code == 200
    assert body["ok"] is True
    assert body["warnings"]  # the failed parse was surfaced
    assert sidecar.exists()
    assert "Some notes." in sidecar.read_text(encoding="utf-8")


def _make_workbook_with_doc_sheet(path):
    workbook = openpyxl.Workbook()
    data = workbook.active
    data.title = "Transactions"
    data.append(("Category", "Amount"))
    data.append(("Groceries", -42.5))

    doc = workbook.create_sheet("Description of Categories")
    doc.append(("Source: some citation",))
    doc.append((None,))
    doc.append(("Category", "Description"))
    doc.append(("Groceries", "Food shopping"))
    workbook.save(path)
    return path


def test_excel_workbook_offers_metadata_and_loads_only_data(tmp_path):
    book = _make_workbook_with_doc_sheet(tmp_path / "book.xlsx")
    app = create_app(str(book), profile=False)
    client = TestClient(app)

    config = client.get("/api/config").json()
    schema = client.get("/api/schema").json()

    assert config["metadata_suggestion"] is True
    # Only the data sheet became a table; the doc sheet was split off.
    assert [t["name"] for t in schema["tables"]] == ["Transactions"]

    # The offer describes which sheet it came from.
    meta = client.get("/api/metadata").json()
    assert meta["pending"]["sheets"] == ["Description of Categories"]

    # Discarding the offer clears the suggestion.
    client.delete("/api/metadata/pending")
    assert client.get("/api/config").json()["metadata_suggestion"] is False

# Made with Bob
