"""Tests for message construction in LLMQueryTranslator.

Not integration tests against a real model - `ollama.chat` is stubbed so
these can assert on exactly what gets sent, which is what actually matters
for BACKLOG.md #2: curated examples must arrive as real prior turns, not as
prose folded into the system prompt.
"""

import json

import pytest

from sql_browser.llm import LLMQueryTranslator


@pytest.fixture(autouse=True)
def no_ollama(monkeypatch):
    """Stop the translator from requiring a live Ollama during these tests."""
    monkeypatch.setattr(
        "sql_browser.llm.LLMQueryTranslator._verify_ollama", lambda self: None
    )


def _stub_chat(monkeypatch, sql="SELECT 1"):
    """Replace ollama.chat and capture the messages it was called with."""
    captured = {}

    def fake_chat(model, messages, format):
        captured["messages"] = messages
        return {"message": {"content": json.dumps({"sql": sql, "explanation": "x", "confidence": "high"})}}

    monkeypatch.setattr("sql_browser.llm.ollama.chat", fake_chat)
    return captured


def test_examples_render_as_prior_turns(monkeypatch):
    captured = _stub_chat(monkeypatch)
    translator = LLMQueryTranslator()

    translator.translate_to_sql(
        "how many orders were shipped",
        "Database Schema:\n",
        examples=[
            {"question": "how many customers are there", "sql": "SELECT COUNT(*) FROM customers"}
        ],
    )

    messages = captured["messages"]
    assert messages[0]["role"] == "system"
    assert messages[1] == {"role": "user", "content": "how many customers are there"}
    assert messages[2]["role"] == "assistant"
    assert json.loads(messages[2]["content"])["sql"] == "SELECT COUNT(*) FROM customers"
    # The real question comes last, after the demonstration.
    assert messages[-1] == {"role": "user", "content": "how many orders were shipped"}


def test_examples_precede_real_conversation_history(monkeypatch):
    captured = _stub_chat(monkeypatch)
    translator = LLMQueryTranslator()

    translator.translate_to_sql(
        "and last week?",
        "schema",
        conversation_history=[
            {"role": "user", "content": "how many orders"},
            {"role": "assistant", "content": "SQL: SELECT COUNT(*) FROM orders"},
        ],
        examples=[{"question": "example question", "sql": "SELECT 1"}],
    )

    roles_and_contents = [(m["role"], m["content"]) for m in captured["messages"]]
    example_index = roles_and_contents.index(("user", "example question"))
    history_index = roles_and_contents.index(("user", "how many orders"))
    assert example_index < history_index


def test_no_examples_means_no_extra_turns(monkeypatch):
    captured = _stub_chat(monkeypatch)
    translator = LLMQueryTranslator()

    translator.translate_to_sql("hi", "schema")

    # Just the system prompt and the current question.
    assert len(captured["messages"]) == 2
    assert captured["messages"][1] == {"role": "user", "content": "hi"}


# --------------------------------------------------------------------------- #
# parse_prose_to_metadata: the single LLM step behind regenerate_structured
# --------------------------------------------------------------------------- #

def _stub_prose_chat(monkeypatch, content):
    """Replace ollama.chat for the prose parser and capture its arguments."""
    captured = {}

    def fake_chat(model, messages, format):
        captured["messages"] = messages
        captured["format"] = format
        return {"message": {"content": content}}

    monkeypatch.setattr("sql_browser.llm.ollama.chat", fake_chat)
    return captured


def test_parse_prose_requests_json_and_sources_prompt_from_context(monkeypatch):
    captured = _stub_prose_chat(monkeypatch, json.dumps({"description": "orders"}))
    translator = LLMQueryTranslator()

    result = translator.parse_prose_to_metadata(
        "table", "status is a code", "Table: orders", table="orders"
    )

    assert result == {"description": "orders"}
    assert captured["format"] == "json"
    # The prompt is the one context builds: it carries the prose and the hint.
    prompt = captured["messages"][0]["content"]
    assert "status is a code" in prompt
    assert "Table: orders" in prompt


def test_parse_prose_repairs_malformed_json(monkeypatch):
    # Trailing comma - invalid JSON that json_repair can fix.
    _stub_prose_chat(monkeypatch, '{"description": "orders",}')
    translator = LLMQueryTranslator()

    result = translator.parse_prose_to_metadata("table", "notes", "hint", table="orders")

    assert result == {"description": "orders"}


def test_parse_prose_returns_empty_dict_on_failure(monkeypatch):
    def boom(model, messages, format):
        raise RuntimeError("ollama down")

    monkeypatch.setattr("sql_browser.llm.ollama.chat", boom)
    translator = LLMQueryTranslator()

    # A failed call must degrade to {} (regenerate turns that into a warning).
    assert translator.parse_prose_to_metadata("database", "notes", "hint") == {}


def test_classify_sheet_returns_only_known_verdicts(monkeypatch):
    _stub_prose_chat(monkeypatch, json.dumps({"kind": "metadata"}))
    translator = LLMQueryTranslator()

    assert translator.classify_sheet("Notes", "BGC | Bank Giro Credit") == "metadata"


def test_classify_sheet_rejects_an_unusable_verdict(monkeypatch):
    _stub_prose_chat(monkeypatch, json.dumps({"kind": "maybe"}))
    translator = LLMQueryTranslator()

    assert translator.classify_sheet("Notes", "sample") is None

# Made with Bob
