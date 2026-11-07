# Handover Note: DuckDB Support and Headless CLI Query Mode for `sql-browser`

**Date:** 2026-11-07  
**Originating Spec in `artmind9`:** `docs/superpowers/specs/2026-11-07-structured-table-relationships-and-sql-retrieval-design.md`  
**Target Repository:** `/Users/surjitdas/projects/sql-browser`  
**Target Branch:** `feat/duckdb-and-headless-cli` (to be created in `sql-browser`)  

---

## 1. Context and Objective

`artmind` (`artmind9`) is a multi-genre knowledge-graph and document ingestion system with a structured store backed by DuckDB and Parquet. 

Rather than duplicating SQL exploration and text-to-SQL logic inside `artmind9`, `artmind9` will delegate structured SQL retrieval directly to `sql-browser` via a non-interactive CLI interface. 

To enable this decoupled architecture, `sql-browser` needs two primary capabilities:
1. **DuckDB Engine Support:** Ability to inspect and query `.duckdb` database files (and Parquet tables) in addition to SQLite and Excel.
2. **Headless / Non-Interactive CLI Query Mode:** A `query` subcommand that takes a question, database, and optional metadata sidecar, runs the full retrieval and self-repair pipeline, and emits clean JSON results to stdout without launching an interactive terminal REPL or web server.

---

## 2. Repo Conventions & Standards (`CLAUDE.md`)

When implementing changes in `sql-browser`, strictly adhere to its existing standards:
- **Toolchain:** Python ≥ 3.10 with `uv` (`uv sync`, `uv add`, `uv run pytest`). Never use `pip`.
- **Typing:** Use explicit types from `typing` (`Optional`, `Dict`, `List`, `Tuple`, etc.).
- **Graceful Degradation:** Never let an unparseable query, missing metadata, or profiling failure crash the session. Degrade to warnings.
- **Additive Metadata:** The physical database schema is always authoritative. Metadata is strictly additive and reconciled.
- **Convention:** End `.py` files with the `# Made with Bob` footer as present in the rest of the codebase.
- **Backward Compatibility:** All existing SQLite (`.db`, `.sqlite`) and Excel workbook (`.xlsx`, `.xlsm`) features, the interactive REPL, and the FastAPI web UI (`--web`) must continue to work with zero regressions.

---

## 3. Step-by-Step Task Breakdown (Phase A)

### Task A1: Implement `DuckDBInspector` in `sql_browser/database.py`

1. **Add Dependency:**
   ```bash
   uv add duckdb
   ```
2. **Refactor Database Layer (`sql_browser/database.py`):**
   - Extract common inspection methods into a base class / protocol `DatabaseInspector`:
     - `connect()`, `disconnect()`
     - `get_tables() -> List[str]`
     - `get_table_schema(table: str) -> List[Dict[str, Any]]`
     - `get_foreign_keys(table: str) -> List[Dict[str, Any]]`
     - `get_distinct_values(table: str, column: str, limit: int) -> List[Any]`
     - `get_table_sample(table: str, limit: int) -> List[Dict[str, Any]]`
   - Implement `DuckDBInspector(DatabaseInspector)`:
     - Connects using `duckdb.connect(str(db_path), read_only=True)`.
     - Tables query: `SELECT table_name FROM information_schema.tables WHERE table_schema='main' ORDER BY table_name`.
     - Columns query: `SELECT column_name, data_type, is_nullable FROM information_schema.columns WHERE table_name = ?`.
     - Foreign keys: Query `information_schema.referential_constraints` / `table_constraints` if defined, or return `[]` (allowing the metadata YAML sidecar to supply relationships).
   - Implement `SQLiteInspector(DatabaseInspector)`:
     - Retains the existing SQLite-specific logic (`sqlite3.connect`, `PRAGMA table_info`, `PRAGMA foreign_key_list`).
   - Implement factory logic in `create_inspector(db_path: Path) -> DatabaseInspector`:
     - Extension `.duckdb` or `.parquet`: returns `DuckDBInspector`.
     - Extension `.db`, `.sqlite`, `.sqlite3`: returns `SQLiteInspector`.
     - Extension `.xlsx`, `.xlsm`: converts to temporary SQLite via `convert_xlsx_to_sqlite()` and returns `SQLiteInspector`.

---

### Task A2: Make SQL Guard & Profiler Dialect-Aware

1. **Dialect in `sql_browser/sql_guard.py`:**
   - Parameterize functions with target dialect: `dialect: str = "sqlite"` (supports `"sqlite"` and `"duckdb"`).
   - Update `transpile_to_dialect(sql: str, dialect: str = "sqlite") -> str`:
     - Uses `sqlglot.transpile(sql, read=None, write=dialect)`.
     - When `dialect == "duckdb"`, skips SQLite-specific compound query repairs that do not apply to DuckDB.
   - Update `is_read_only(sql: str, dialect: str)`:
     - Uses `sqlglot.parse(sql, read=dialect)`.
     - Enforces read-only statements (`exp.Select`, `exp.Union`).
   - Update `enforce_limit(sql: str, limit: int, dialect: str)`:
     - Appends `LIMIT` using `dialect`.
   - Update `find_schema_problems(sql: str, schema: Dict[str, Dict[str, str]], dialect: str)`:
     - Uses `sqlglot.optimizer.qualify(expression, schema=schema, dialect=dialect)`.

2. **Profiling in `sql_browser/profiler.py`:**
   - In DuckDB, profile caching can create an in-memory or persisted table `_sql_browser_profile`, or store results in the inspector instance.
   - Ensure null rate, distinct enums, and min/max range queries run cleanly on DuckDB.

---

### Task A3: Add Non-Interactive Headless Query Command (`sql-browser query`)

In `sql_browser/cli.py`, add a new Typer command `query`:

```bash
sql-browser query "<question>" <database_path> \
  [--metadata <metadata_path>] \
  [--no-metadata] \
  [--model <model>] \
  [--json] \
  [--dry-run]
```

#### Execution Flow in `query`:
1. **Initialize Inspector:** Inspect database using `create_inspector(database_path)`.
2. **Resolve Metadata:** Call `resolve_metadata(inspector, metadata_path)`.
3. **Relevance Pruning:** Call `relevant_tables(question, schema, metadata)` with 1-hop join expansion.
4. **Build Prompt:** Compose schema, explicit `RELATIONSHIPS`, `GLOSSARY`, `RULES`, and column profile enums via `context.py`.
5. **LLM Translation:** Call `LLMQueryTranslator.translate_to_sql()`.
6. **SQL Guard & Repair:**
   - Parse and transpile using target dialect (`sqlite` or `duckdb`).
   - Validate read-only.
   - Run `find_schema_problems()`. If errors exist, invoke `repair_sql()` for 1 turn.
7. **Literal Value Correction:** Run `query_corrector.py` against the inspector to fix case mismatches or map labels to codes.
8. **Enforce Limit:** Apply `enforce_limit(sql, limit=1000, dialect=dialect)`.
9. **Execute & Output:**
   - If `--dry-run`: skip execution.
   - Execute query on connection.
   - If `--json`: output structured JSON to stdout:
     ```json
     {
       "question": "Show me total claims by region",
       "sql": "SELECT region, SUM(amount) AS total_amount FROM claims GROUP BY region",
       "rows": [
         {"region": "North", "total_amount": 142050.0},
         {"region": "South", "total_amount": 98200.0}
       ],
       "corrections": [],
       "notes": "Curated example.",
       "warnings": []
     }
     ```
   - If execution fails at runtime, invoke `repair_sql()` with runtime error message, re-execute once, and return.
   - Exit with code 0 on success, non-zero on fatal failure.

---

### Task A4: Add Metadata Subcommands

In `sql_browser/cli.py`, add explicit subcommands for managing sidecar metadata without opening a session:

1. **Initialize Metadata:**
   ```bash
   sql-browser metadata init <database_path> --out <metadata_path> [--llm-draft] [--model <model>]
   ```
   - Generates the starter YAML using `build_scaffold()`.
2. **Validate Metadata:**
   ```bash
   sql-browser metadata validate <database_path> --metadata <metadata_path>
   ```
   - Runs `reconcile()` against the live schema and outputs warnings for missing tables, columns, or ambiguous value labels.
3. **Regenerate Metadata from Prose:**
   ```bash
   sql-browser metadata regenerate <database_path> --metadata <metadata_path> [--model <model>]
   ```
   - Runs `regenerate_structured()` to update structured YAML stanzas from free-form `prose:`.

---

### Task A5: Test Suite & Verification

Create the following tests under `sql-browser/tests/`:
1. `tests/test_duckdb.py`:
   - Creates a temporary DuckDB database using `duckdb.connect()`.
   - Creates tables and inserts test rows.
   - Verifies `DuckDBInspector` correctly extracts tables, columns, sample data, and distinct enum values.
2. `tests/test_cli_query.py`:
   - Runs CLI tests using Typer's `CliRunner`.
   - Tests `sql-browser query "..." test.duckdb --json --dry-run`.
   - Tests `sql-browser metadata init test.duckdb --out test.meta.yaml`.
   - Tests `sql-browser metadata validate test.duckdb --metadata test.meta.yaml`.
3. Run the full test suite:
   ```bash
   uv run pytest
   ```

---

## 4. Next Step After Phase A

Once Phase A is complete in `sql-browser`:
- In `artmind9`, `artmind/text2sql.py` will be wired to invoke `sql-browser query "<question>" <duckdb_file> --metadata <vault_meta_path> --json`.
- `artmind` will overlay its Knowledge Graph fusion hints (`[bridge]`, `[→ CLASS]`) onto the output, completing the integration.
