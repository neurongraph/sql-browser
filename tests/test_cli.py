"""Tests for the CLI's metadata wiring.

Covers the two things that are easy to get wrong and invisible when they are:
that contradictory flags are refused rather than silently resolved, and that
reloading metadata mid-session actually rebuilds the text sent to the model -
not just what the `metadata` command prints.

`LLMQueryTranslator.__init__` pings Ollama, so these tests stub that out: none
of this behaviour involves a model, and requiring a running one would make the
suite environment-dependent.
"""

import pytest
from typer.testing import CliRunner

from sql_browser import cli as cli_module
from sql_browser.cli import SQLBrowserSession, app
from sql_browser.query_executor import QueryExecutor

runner = CliRunner()


@pytest.fixture(autouse=True)
def no_ollama(monkeypatch):
    """Stop the translator from requiring a live Ollama during these tests."""
    monkeypatch.setattr(
        "sql_browser.llm.LLMQueryTranslator._verify_ollama", lambda self: None
    )


# --------------------------------------------------------------------------- #
# Flag validation
# --------------------------------------------------------------------------- #

def test_metadata_and_no_metadata_are_refused(db_path):
    """Silently ignoring the path you explicitly passed is the worst outcome."""
    result = runner.invoke(
        app, ["--metadata", str(db_path), "--no-metadata", str(db_path)]
    )

    assert result.exit_code == 1
    assert "contradict" in result.output


def test_llm_draft_without_init_metadata_is_refused(db_path):
    result = runner.invoke(app, ["--llm-draft", str(db_path)])

    assert result.exit_code == 1
    assert "--init-metadata" in result.output


def test_missing_metadata_override_is_reported_not_ignored(db_path, tmp_path):
    """A path that doesn't exist should say so rather than quietly do nothing."""
    session = SQLBrowserSession(
        str(db_path), profile=False, metadata_path=tmp_path / "absent.meta.yaml"
    )
    session.db_inspector.connect()
    session.executor = QueryExecutor(session.db_inspector)
    try:
        session._load_metadata()
    finally:
        session.db_inspector.disconnect()

    assert any("not found" in w for w in session.db_inspector.metadata_warnings)


# --------------------------------------------------------------------------- #
# init-metadata
# --------------------------------------------------------------------------- #

def test_init_metadata_writes_a_sidecar(db_path):
    result = runner.invoke(app, ["--init-metadata", str(db_path)])

    sidecar = db_path.with_name("shop.meta.yaml")
    assert result.exit_code == 0
    assert sidecar.is_file()
    assert "orders:" in sidecar.read_text()


def test_init_metadata_refuses_to_overwrite(db_path):
    """Never clobber documentation someone wrote by hand."""
    sidecar = db_path.with_name("shop.meta.yaml")
    sidecar.write_text('version: 1\ndatabase:\n  description: "mine"\n')

    result = runner.invoke(app, ["--init-metadata", str(db_path)])

    assert result.exit_code == 1
    assert "already exists" in result.output
    assert "mine" in sidecar.read_text()  # untouched


# --------------------------------------------------------------------------- #
# regenerate-metadata (the CLI twin of the web editor's Save)
# --------------------------------------------------------------------------- #

def _stub_parse(monkeypatch, response):
    """Stub parse_prose_to_metadata so no live model is needed."""
    def fake(self, section, prose_text, schema_hint, table=None):
        return response

    monkeypatch.setattr(
        "sql_browser.llm.LLMQueryTranslator.parse_prose_to_metadata", fake
    )


def test_regenerate_metadata_rebuilds_structured_from_prose(db_path, monkeypatch):
    """A prose edit becomes real structured metadata, reconciled against the data."""
    # A stanza with one valid code (P, in the data) and one invented (Z).
    _stub_parse(
        monkeypatch,
        {
            "description": "orders",
            "columns": {
                "status": {"values": {"P": {"label": "Pending"}, "Z": {"label": "Nope"}}}
            },
        },
    )
    sidecar = db_path.with_name("shop.meta.yaml")
    sidecar.write_text(
        "version: 1\n"
        "prose:\n"
        "  tables:\n"
        "    orders:\n"
        "      P means Pending.\n"
    )

    result = runner.invoke(app, ["--regenerate-metadata", str(db_path)])

    assert result.exit_code == 0
    on_disk = sidecar.read_text()
    # RAW structured is written (reconcile is a read-time gate): the generated
    # label lands on disk, and provenance ties it back to the prose.
    assert "from_prose_digest:" in on_disk
    assert "Pending" in on_disk
    # The reconcile report tells the user the invented code was dropped.
    assert "1 dropped" in result.output


def test_regenerate_metadata_refuses_without_a_sidecar(db_path):
    result = runner.invoke(app, ["--regenerate-metadata", str(db_path)])

    assert result.exit_code == 1
    assert "no metadata file" in result.output


def test_regenerate_metadata_refuses_when_prose_is_empty(db_path):
    """A legacy structured-only file has nothing to regenerate from."""
    sidecar = db_path.with_name("shop.meta.yaml")
    sidecar.write_text('version: 1\ndatabase:\n  description: "hand-authored"\n')

    result = runner.invoke(app, ["--regenerate-metadata", str(db_path)])

    assert result.exit_code == 1
    assert "no `prose:` block" in result.output
    assert "hand-authored" in sidecar.read_text()  # untouched


def test_init_and_regenerate_are_mutually_exclusive(db_path):
    result = runner.invoke(
        app, ["--init-metadata", "--regenerate-metadata", str(db_path)]
    )

    assert result.exit_code == 1
    assert "one or the other" in result.output


# --------------------------------------------------------------------------- #
# Mid-session reload
# --------------------------------------------------------------------------- #

def test_metadata_command_reloads_from_disk(db_path):
    """The authoring loop is write, ask, refine - a restart per edit is friction.

    Asserts on `schema_info`, not on what was printed: metadata only reaches the
    model through that text, so a reload that refreshes the display without
    rebuilding it would look right and change nothing.
    """
    sidecar = db_path.with_name("shop.meta.yaml")
    sidecar.write_text('version: 1\ndatabase:\n  description: "First version."\n')

    session = SQLBrowserSession(str(db_path), profile=False)
    session.db_inspector.connect()
    session.executor = QueryExecutor(session.db_inspector)
    try:
        session._load_metadata()
        session.schema_info = session.db_inspector.get_schema_description()
        assert "First version." in session.schema_info

        # Edit the file as a user would, then reload without restarting.
        sidecar.write_text(
            'version: 1\n'
            'database:\n'
            '  description: "Second version."\n'
            '  rules:\n'
            '    - "A brand new rule."\n'
        )
        handled = session._handle_special_command("metadata")

        assert handled is True
        assert "Second version." in session.schema_info
        assert "A brand new rule." in session.schema_info
        assert "First version." not in session.schema_info
    finally:
        session.db_inspector.disconnect()


def test_reload_surfaces_newly_introduced_warnings(db_path):
    sidecar = db_path.with_name("shop.meta.yaml")
    sidecar.write_text("version: 1\n")

    session = SQLBrowserSession(str(db_path), profile=False)
    session.db_inspector.connect()
    session.executor = QueryExecutor(session.db_inspector)
    try:
        session._load_metadata()
        assert session.db_inspector.metadata_warnings == []

        sidecar.write_text(
            'version: 1\ntables:\n  ghost:\n    description: "not real"\n'
        )
        session._handle_special_command("metadata")

        assert any("ghost" in w for w in session.db_inspector.metadata_warnings)
        assert "not real" not in session.schema_info
    finally:
        session.db_inspector.disconnect()


def test_no_metadata_session_still_reloads_cleanly(db_path):
    """With --no-metadata the command must still work, just find nothing."""
    session = SQLBrowserSession(str(db_path), profile=False, use_metadata=False)
    session.db_inspector.connect()
    session.executor = QueryExecutor(session.db_inspector)
    try:
        session._load_metadata()
        db_path.with_name("shop.meta.yaml").write_text(
            'version: 1\ndatabase:\n  description: "Ignored."\n'
        )
        session._handle_special_command("metadata")

        assert "Ignored." not in session.schema_info
        # Foreign keys are database facts, so they survive --no-metadata.
        assert session.db_inspector.metadata.relationships
    finally:
        session.db_inspector.disconnect()


# --------------------------------------------------------------------------- #
# Per-question schema pruning and example selection (BACKLOG.md #1, #2)
# --------------------------------------------------------------------------- #

def test_query_context_uses_the_question_to_prune_and_pick_examples(db_path):
    """The per-turn helper, not just self.schema_info, must carry both."""
    sidecar = db_path.with_name("shop.meta.yaml")
    sidecar.write_text(
        "version: 1\n"
        "examples:\n"
        "  - question: how many orders are there\n"
        "    sql: SELECT COUNT(*) FROM orders\n"
    )

    session = SQLBrowserSession(str(db_path), profile=False)
    session.db_inspector.connect()
    session.executor = QueryExecutor(session.db_inspector)
    try:
        session._load_metadata()

        schema_info, examples = session._query_context("how many orders are there")

        assert "Table: orders" in schema_info
        assert examples == [
            {"question": "how many orders are there", "sql": "SELECT COUNT(*) FROM orders"}
        ]
    finally:
        session.db_inspector.disconnect()


def test_query_context_without_metadata_returns_no_examples(db_path):
    session = SQLBrowserSession(str(db_path), profile=False, use_metadata=False)
    session.db_inspector.connect()
    session.executor = QueryExecutor(session.db_inspector)
    try:
        session._load_metadata()

        _, examples = session._query_context("how many orders are there")

        assert examples == []
    finally:
        session.db_inspector.disconnect()


def test_profile_command_rebuilds_the_prompt(db_path):
    """Profiling on demand has to reach the model too, not just the display."""
    session = SQLBrowserSession(str(db_path), profile=False)
    session.db_inspector.connect()
    session.executor = QueryExecutor(session.db_inspector)
    try:
        session._load_metadata()
        session.schema_info = session.db_inspector.get_schema_description()
        assert "(values:" not in session.schema_info

        session._handle_special_command("profile")

        assert "(values:" in session.schema_info
    finally:
        session.db_inspector.disconnect()

# Made with Bob
