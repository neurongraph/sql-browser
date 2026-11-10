"""The OpenAI-compatible provider: requests go through the openai client with no
temperature or response_format, replies come back in Ollama's shape, and the
Ollama check is skipped."""
from types import SimpleNamespace

import pytest

from sql_browser import llm
from sql_browser.config import settings


@pytest.fixture
def openai_provider(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(settings, "llm_base_url", "https://gateway.example/v1")
    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    calls = []

    class FakeCompletions:
        def create(self, **kwargs):
            calls.append(kwargs)
            msg = SimpleNamespace(content='{"sql": "SELECT 1", "explanation": "one"}')
            return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    fake = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
    monkeypatch.setattr(llm, "_openai_client", fake)
    return calls


def test_chat_uses_openai_shape_and_no_extra_params(openai_provider):
    out = llm._chat("some-model", [{"role": "user", "content": "q"}])
    assert out["message"]["content"].startswith("{")
    assert openai_provider == [{"model": "some-model", "messages": [{"role": "user", "content": "q"}]}]


def test_translator_skips_ollama_check_and_translates(openai_provider, monkeypatch):
    def boom():
        raise AssertionError("ollama must not be contacted")
    monkeypatch.setattr(llm.ollama, "list", boom)
    t = llm.LLMQueryTranslator(model="some-model")
    result = t.translate_to_sql("how many rows", "TABLE t (a INTEGER)")
    assert result["sql"].strip().upper().startswith("SELECT")


def test_openai_without_settings_raises(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(settings, "llm_base_url", "")
    monkeypatch.setattr(settings, "llm_api_key", "")
    monkeypatch.setattr(llm, "_openai_client", None)
    with pytest.raises(RuntimeError, match="SQL_BROWSER_LLM_BASE_URL"):
        llm._chat("m", [{"role": "user", "content": "q"}])
