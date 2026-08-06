# SQL Browser Usage Guide

## Quick Start

1. **Install Ollama** (if not already installed):
   ```bash
   # Visit https://ollama.ai/ and follow installation instructions
   # Then pull a model:
   ollama pull gemma4:31b-cloud
   ```

2. **Install dependencies**:
   ```bash
   uv sync
   ```

3. **Create a sample database** (optional):
   ```bash
   uv run python examples/create_sample_db.py
   ```

4. **Run SQL Browser**:
   ```bash
   # Terminal REPL
   uv run sql-browser sample.db

   # Or launch the browser-based web UI
   uv run sql-browser --web sample.db

   # The file is optional with --web - pick or upload one in the browser
   uv run sql-browser --web
   ```

## Interfaces

SQL Browser has two front-ends over the same core engine:

- **Terminal REPL** (default) - type questions at the `🔍` prompt, get
  Rich-formatted tables, use special commands like `schema` and `profile`.
  Requires a database or workbook argument.
- **Web UI** (`--web`) - a localhost browser app with file and model selectors, a
  schema sidebar, suggestion chips, an **editable** SQL box, a results table, and
  a value picker for correcting guessed filter values. Open the printed URL
  (default http://127.0.0.1:8000); change it with `--port`. The file argument is
  optional here.

Both accept a SQLite database **or** an Excel workbook (`.xlsx`/`.xlsm`), where
each sheet is loaded as a table into a temporary SQLite database for the session:

```bash
uv run sql-browser sales.xlsx
uv run sql-browser --web sales.xlsx
```

With `just`:

```bash
just run mydata.db                 # terminal REPL
just web mydata.db                 # web UI (default model, port 8000)
just web mydata.xlsx granite4.1:8b 8137 # web UI with custom model + port
just web-blank                     # web UI with nothing preselected
```

## Choosing the file and model in the web UI

The web UI's header has a **File** selector, an **Upload…** button, a **Model**
selector, and **Load**. Anything passed on the command line is pre-selected;
otherwise the file selector starts empty and the model selector lists whatever
`ollama list` reports.

- **Upload…** copies a `.db`/`.sqlite`/`.sqlite3`/`.xlsx`/`.xlsm` file into the
  data directory (`data/` by default) and loads it right away. This is how files
  get into the selector: a browser can't hand a server a real filesystem path.
- A file passed on the command line is read **in place** - never copied - and
  appears in the selector labelled `(command line)`.
- Switching the **model** is cheap: your transcript, schema, and profile stay put.
- Switching the **file** re-profiles it and clears the transcript, since past
  questions and their SQL refer to a different database.
- **Load** re-opens and re-profiles the current file, which is what you want when
  the database changed on disk.
- If a file fails to load (corrupt, or a workbook whose layout can't be
  converted), the error is shown and the previously loaded database stays usable.

Configuration lives in `.env` (copy `.env.example`); every key is optional:

| Key | Default | Purpose |
| --- | --- | --- |
| `SQL_BROWSER_DATA_DIR` | `data` | Where uploads are stored (also `--data-dir`) |
| `SQL_BROWSER_DEFAULT_MODEL` | `gemma4:31b-cloud` | Model used when none is given |
| `SQL_BROWSER_MAX_UPLOAD_MB` | `256` | Upload size limit |

## Example Queries

Once the application is running, you can ask questions in natural language:

### Basic Queries
- "Show me all tables"
- "What are the columns in the customers table?"
- "Show me 5 sample customers"

### Data Exploration
- "How many customers do we have?"
- "List all products in the Electronics category"
- "Show me the top 10 most expensive products"
- "Find all orders from the last 30 days"

### Aggregations
- "What is the total revenue from all orders?"
- "Show me the average order value"
- "Count how many orders each customer has made"
- "What are the top 5 best-selling products?"

### Filtering
- "Find customers from California"
- "Show me all pending orders"
- "List products with stock less than 50"
- "Find orders with total amount greater than $500"

### Joins
- "Show me customer names with their order totals"
- "List all order items with product names"
- "Find which customers bought laptops"

## Special Commands

While in the interactive terminal session:

- `schema` - Display the complete database schema
- `tables` - List all tables in the database
- `profile` - Show (or refresh) profiled column values/ranges
- `clear` - Clear conversation history
- `logs` - Show logs directory location
- `help` - Show help message
- `exit` or `quit` - Exit the application

## Tips

1. **Be Specific**: The more specific your question, the better the results
   - ❌ "Show me data"
   - ✅ "Show me the top 10 customers by total order value"

2. **Context Matters**: The AI maintains conversation context
   - First: "Show me all customers from California"
   - Then: "How many orders have they made?" (refers to CA customers)

3. **Check Confidence**: Low confidence queries will show a warning
   - Review the generated SQL to ensure it matches your intent

4. **Read-Only**: Only SELECT queries are allowed for safety
   - Queries are parsed and validated; INSERT/UPDATE/DELETE/DROP and stacked
     statements are rejected
   - This applies to SQL you type or edit yourself in the web UI too

5. **Schema Awareness**: Use `schema` command to see available tables and columns
   - Helps you ask more accurate questions

6. **Value Correction**: If the model filters on a value that isn't in your data
   (e.g. `status = 'Opne'`), SQL Browser detects it and either auto-corrects a
   close match or offers you the real values to choose from - a prompt in the
   terminal, a picker in the web UI. Works with `=`, `!=`, and `IN (...)`, and
   is JOIN-aware.

7. **Profiling**: On startup, low-cardinality columns and numeric ranges are
   profiled and shown to the model as hints, which sharpens value accuracy.
   Skip it with `--no-profile`, or refresh it with the `profile` command.

## Using Different Models

You can specify a different Ollama model:

```bash
uv run sql-browser --model granite4.1:8b sample.db
```

In the web UI you can switch models from the header's **Model** selector at any
time without restarting - it lists whatever `ollama list` reports, and switching
keeps your transcript and schema.

Available models (must be pulled first with `ollama pull <model>`):
- `gemma4:31b-cloud` (default, recommended)
- `gemma4:e4b`
- `granite4.1:8b`
- `phi4`

The default can be changed for good with `SQL_BROWSER_DEFAULT_MODEL` in `.env`.

## Troubleshooting

### "Failed to connect to Ollama"
- Make sure Ollama is running: `ollama serve`
- Check if the model is available: `ollama list`
- Pull the model if needed: `ollama pull gemma4:31b-cloud`

### "Database file not found"
- Verify the path to your database file
- Use absolute path if relative path doesn't work
- Create sample database: `uv run python examples/create_sample_db.py`

### Low Quality Results
- Try being more specific in your questions
- Use the `schema` command to understand the database structure
- Try a different model with `--model` option
- Clear conversation history with `clear` command

### Import Errors
- Run `uv sync` to install all dependencies
- Make sure you're using Python 3.10 or higher

## Advanced Usage

### Using with Your Own Database or Workbook

```bash
uv run sql-browser /path/to/your/database.db
uv run sql-browser /path/to/your/workbook.xlsx      # sheets -> tables
uv run sql-browser --web /path/to/your/database.db  # browser UI
```

### Programmatic Usage

You can also use SQL Browser as a library. The example below wires in the same
safety pipeline the CLI and web UI use:

```python
from sql_browser.database import DatabaseInspector
from sql_browser.llm import LLMQueryTranslator
from sql_browser.query_executor import QueryExecutor
from sql_browser.query_corrector import resolve_value_mismatches
from sql_browser.sql_guard import (
    build_schema, transpile_to_sqlite, is_read_only,
    find_schema_problems, enforce_limit,
)

db = DatabaseInspector("mydata.db")
llm = LLMQueryTranslator(model="gemma4:31b-cloud")

with db:
    executor = QueryExecutor(db)
    schema_info = db.get_schema_description()

    # 1. Translate natural language to SQL
    result = llm.translate_to_sql("Show me all customers", schema_info)
    sql = transpile_to_sqlite(result["sql"])

    # 2. Reject anything that isn't read-only
    if not is_read_only(sql):
        raise ValueError("Only read-only SELECT queries are allowed")

    # 3. Catch hallucinated tables/columns; repair once if needed
    problems = find_schema_problems(sql, build_schema(db))
    if problems:
        repaired = llm.repair_sql(
            "Show me all customers", schema_info, sql, "; ".join(problems)
        )
        sql = transpile_to_sqlite(repaired["sql"])

    # 4. Correct guessed literal values (auto-picks the first candidate here)
    sql = resolve_value_mismatches(
        sql, db, ask_choice=lambda col, val, candidates: candidates[0]
    )

    # 5. Cap unbounded results, then execute
    executor.execute_and_display(enforce_limit(sql), result["explanation"])
```

### Embedding the web app

`sql_browser.web.create_app(db_path=None, model=None, profile=True)` returns a
FastAPI app you can serve yourself, and `run_web(db_path=None, ...)` starts
uvicorn for you. Both arguments are optional: with no `db_path` the app starts
unbound and the file is chosen over the API (`POST /api/session`,
`POST /api/upload`) or in the UI. The mutable file/model state lives in a
`BrowserSession`, so re-binding doesn't require restarting the server.

## Examples with Sample Database

After creating the sample database, try these queries:

```
🔍 Show me all customers from California

🔍 What are the top 5 products by price?

🔍 How many orders were placed in the last 30 days?

🔍 Show me the total revenue by product category

🔍 Find customers who have spent more than $1000

🔍 List all delivered orders with customer names

🔍 What is the average order value?

🔍 Show me products that are low in stock (less than 30)

🔍 Which customers have never placed an order?

🔍 What are the most popular products by quantity sold?