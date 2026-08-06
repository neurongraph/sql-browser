# SQL Browser — working notes

A natural-language SQLite/Excel browser driven by a local LLM via Ollama. Ask a
question in English, get SQL, get results — in a Rich terminal REPL or a
FastAPI-backed web UI.

## Toolchain

Python ≥3.10, `uv` for everything. Never `pip`.

```bash
uv sync                                  # install (incl. dev group)
uv run sql-browser sample.db             # terminal REPL
uv run sql-browser --web sample.db       # web UI
uv run pytest tests/ -q                  # tests
uv run python examples/create_sample_db.py   # writes sample.db to the CWD
```

Entry point is `sql_browser.cli:main`. There's also a `justfile` with shortcuts
(`just run`, `just web`, `just run-sample`).

## Layout

Flat `sql_browser/`, one concern per module, no subpackages. Read the module
docstring before changing a module — several explain a design rationale that
isn't obvious from the code.

| Module | Concern |
| --- | --- |
| `database.py` | Connection, `PRAGMA` schema + foreign key inspection |
| `profiler.py` | Per-column enum/range profiling, cached in the DB |
| `metadata.py` | Curated YAML metadata: load, reconcile, scaffold |
| `context.py` | Renders schema + profile + FKs + metadata into prompt text |
| `llm.py` | Ollama calls: translate, repair, suggest, draft metadata |
| `sql_guard.py` | sqlglot transpile / read-only validation / row limit |
| `query_corrector.py` | Detect and repair guessed literal filter values |
| `query_executor.py` | Execution plus *all* Rich/terminal formatting |
| `excel_loader.py` | Workbook → temporary SQLite database |
| `cli.py` | Typer CLI and the REPL session |
| `web.py` | FastAPI app; `static/` is the no-build frontend |
| `config.py` | pydantic-settings, `SQL_BROWSER_` env prefix |
| `logger.py` | Loguru setup; use `get_logger(__name__)` |

## Conventions

- **Module docstrings explain *why* the module exists**, not just what's in it —
  see `sql_guard.py` and `profiler.py` as the reference. Public functions use
  Google-style `Args:` / `Returns:` / `Raises:`.
- **Comment the non-obvious.** The codebase is fairly dense with comments that
  explain a subtlety or a trap (e.g. why `typer.Exit` must be re-raised before
  the generic `RuntimeError` handler in `cli.py`; why `qualify()` runs on a
  copy in `sql_guard.py`). Match that; don't narrate the obvious.
- **Typing:** `from typing import Optional, Dict, List, Tuple`. The codebase is
  almost entirely on the `typing` module rather than builtin generics — follow
  the dominant style.
- **Trailing `# Made with Bob`** ends nearly every `.py` file. Keep it where it
  is and add it to new modules.

## Design principles

These are load-bearing. Breaking one is a real regression even if tests pass.

**Graceful degradation.** No subsystem may make a session unstartable. Existing
examples: `sql_guard` returns the original SQL rather than blocking when it
can't parse; `llm.list_models()` returns `[]` when Ollama is down; `web.py`
catches profiling failure and warns; `metadata.resolve_metadata()` turns every
failure into a warning. Prefer a degraded session with a warning over an
exception.

**Read-only forever.** All generated SQL passes `sql_guard.is_read_only()`.
Never add a write path.

**Metadata is additive, never authoritative.** Types, keys and the set of values
a column contains always come from the database. Curated metadata only attaches
meaning to what's already there, and `metadata.reconcile()` drops anything that
doesn't resolve against the live schema. This is what lets every downstream
consumer trust metadata without re-checking it. Don't add a path that lets the
file override a physical fact.

**Prompt text is a shared resource.** All of it is composed in `context.py`, so
decisions about what to include and what to truncate are made in one place.
Don't build prompt fragments elsewhere.

**SQL construction:** double-quote identifiers, bind values as parameters. See
`database.get_distinct_values()` and `query_corrector._fuzzy_like_search()`.

**Caching:** the profiler's pattern is a sidecar table inside the SQLite file
keyed by row count, so a changed table re-computes and an unchanged one loads
from cache.

**LLM calls:** log prompt size and duration (`llm.py` `_request_sql`), request
`format="json"`, and parse with `json.loads` falling back to `repair_json`.
Prefer one request per unit of work over one giant request — see
`draft_metadata`, which is per-table so a single bad response costs one stanza.

## The CLI is one command with mode flags

`cli.py` is a single `@app.command`, not a subcommand group. `--web` and
`--init-metadata` are mode flags that do their thing and return. Follow that
pattern rather than restructuring into subcommands.

## Testing

`tests/` uses a real temporary SQLite database (`conftest.py`) rather than
mocks, because most of the logic worth testing is the agreement between code and
a live schema. `pytest` is in the dev dependency group.
