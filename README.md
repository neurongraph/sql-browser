

# SQL Browser

A natural language SQLite database explorer powered by local LLM (Ollama).

## Features

- 🗣️ Natural language queries - ask questions in plain English
- 🖥️ Two interfaces - a rich terminal REPL **and** a browser-based web UI (`--web`)
- 🔍 Automatic schema inspection - understands your database structure
- 🤖 Local LLM powered - uses Ollama (no API keys required)
- 📈 Excel workbook support - open `.xlsx`/`.xlsm` files directly (each sheet becomes a table)
- 🧭 Database profiling - learns each column's real values/ranges to guide the LLM
- 🛡️ SQL safety layer (sqlglot) - dialect normalization, strict read-only validation, and schema-aware checks that catch hallucinated tables/columns before execution
- 🔁 Self-repair - when a query references something that doesn't exist, the specific error is fed back for one automatic fix
- 🎯 Value correction - detects guessed literal values that aren't in the data (`=`, `!=`, `IN (...)`, JOIN-aware) and lets you pick the real one
- 📊 Rich CLI output - formatted tables and results
- 📝 Comprehensive logging with Loguru - debug issues easily

## Prerequisites

- Python 3.10+
- [uv](https://github.com/astral-sh/uv) - Fast Python package installer
- [Ollama](https://ollama.ai/) - Local LLM runtime

## Installation

1. Install Ollama and pull the model:
```bash
# Install Ollama from https://ollama.ai/
ollama pull gemma4:31b-cloud
```

2. Install the project:
```bash
uv sync
```

## Usage

```bash
# Run the terminal REPL against a SQLite database
uv run sql-browser path/to/your/database.db

# Open an Excel workbook (each sheet is loaded as a table)
uv run sql-browser path/to/your/workbook.xlsx

# Launch the browser-based web UI instead of the terminal
uv run sql-browser --web path/to/your/database.db

# The file is optional with --web: upload or pick one in the browser instead
uv run sql-browser --web

# Pick a different model, port, or skip profiling
uv run sql-browser --model granite4.1:8b mydata.db
uv run sql-browser --web --port 8137 mydata.db
uv run sql-browser --no-profile mydata.db

# Example queries:
# - "Show me all tables"
# - "What are the columns in the users table?"
# - "Find all users created in the last month"
# - "Show me the top 10 products by price"
```

### With `just`

```bash
just run mydata.db                 # terminal REPL
just run-no-profile mydata.db      # skip profiling
just web mydata.db                 # web UI (default model, port 8000)
just web mydata.xlsx granite4.1:8b 8137 # web UI with custom model + port
just web-blank                     # web UI with nothing preselected
just run-sample                    # build the sample DB and open it
```

### Command-line options

| Option | Description |
| --- | --- |
| `--model`, `-m` | Ollama model to use (default `gemma4:31b-cloud`, or `SQL_BROWSER_DEFAULT_MODEL`) |
| `--web` | Launch the browser-based UI instead of the terminal REPL |
| `--port` | Port for the web UI (default `8000`, only used with `--web`) |
| `--data-dir` | Where the web UI stores uploaded files (default `data/`) |
| `--no-profile` | Skip profiling the database (column values/ranges) for text-to-SQL hints |
| `--version`, `-v` | Show version and exit |

The `database` argument is required for the terminal REPL but optional with
`--web`, where the file can be chosen in the browser.

## Configuration

Everything has a default, so no configuration is needed. To change one, copy
`.env.example` to `.env` (git-ignored) and edit it, or export the equivalent
variable:

| Key | Default | Purpose |
| --- | --- | --- |
| `SQL_BROWSER_DATA_DIR` | `data` | Where the web UI stores uploaded files |
| `SQL_BROWSER_DEFAULT_MODEL` | `gemma4:31b-cloud` | Model used when none is given |
| `SQL_BROWSER_MAX_UPLOAD_MB` | `256` | Upload size limit |

## Web UI

Run with `--web` and open the printed URL (default http://127.0.0.1:8000). It is
localhost-only and single-user. The interface provides:

- **File and model selectors** in the header. Both are pre-selected from the
  command line when given; otherwise the file selector starts empty and the
  model selector lists everything `ollama list` reports. Switching the model
  keeps your transcript and schema; switching the file resets the transcript.
  **Load** re-opens and re-profiles the current file, for a database that
  changed on disk.
- **Upload…** adds a `.db`/`.sqlite`/`.sqlite3`/`.xlsx`/`.xlsm` file to the data
  directory and loads it immediately. Browsers can't hand a server a real
  filesystem path, so uploading is how files get into the selector - a file
  passed on the command line is read in place and never copied.
- A **schema sidebar** listing tables and columns (with profiled value/range hints).
- A pinned **natural-language box** and clickable **suggestion chips**.
- A **scrollable transcript**: each question appends a new entry with its own
  generated **SQL in an editable box** (tweak it and re-run with **Run SQL**) and
  its own **results table**, so you can scroll back through everything you asked.
- A **value picker**: when the model guesses a filter value that isn't in the
  data, the UI shows the real candidate values so you can pick one and re-run.

Every query - whether generated or hand-edited - passes through the same
read-only safety checks as the CLI, and unbounded results are capped with a
`LIMIT` automatically.

## Special Commands

While in the interactive terminal session:

- `schema` - Display the complete database schema
- `tables` - List all tables in the database
- `profile` - Show (or refresh) profiled column values/ranges
- `clear` - Clear conversation history
- `logs` - Show logs directory location
- `help` - Show help message
- `exit` or `quit` - Exit the application

## How queries are made safe and accurate

Between the model generating SQL and the query running, SQL Browser applies a
deterministic pipeline (shared by the CLI and web UI):

1. **Dialect normalization** - the query is transpiled to SQLite via
   [sqlglot](https://github.com/tobymao/sqlglot), fixing cross-dialect idioms.
2. **Read-only validation** - the query is *parsed* (not keyword-scanned) and
   rejected unless it's a single read-only `SELECT`/`UNION`. This blocks stacked
   statements like `SELECT ...; DROP TABLE x` and no longer false-rejects
   columns such as `created_at`/`updated_at`.
3. **Schema-aware check + repair** - columns and tables are bound against the
   real schema. If the model hallucinated a name, the specific error is fed back
   for a single automatic repair attempt.
4. **Value correction** - literal filter values (`=`, `!=`/`<>`, `IN (...)`,
   JOIN-aware) that don't exist in the column are detected. Close matches (e.g.
   `'open'` vs `'Open'`) are auto-corrected; ambiguous ones prompt you to pick a
   real value (a terminal prompt in the CLI, a picker in the web UI).
5. **Row cap** - a `LIMIT` is injected into unbounded top-level `SELECT`s.

Each step degrades gracefully: if a query can't be parsed, it falls back rather
than blocking an otherwise-valid query.

## Logging

SQL Browser uses Loguru for comprehensive logging:

- **Log Location**: `~/.sql_browser/logs/`
- **Log Files**: `sql_browser_YYYY-MM-DD.log`
- **Retention**: 7 days
- **Rotation**: Daily

Logs capture:
- All user queries
- LLM prompts and responses
- SQL queries generated
- Execution results
- Errors and exceptions with full stack traces

View logs location:
```bash
# In the interactive session, type:
logs
```

Or directly:
```bash
ls ~/.sql_browser/logs/
```

## Development

```bash
# Install with dev dependencies
uv sync --dev

# Run tests
uv run pytest

# Run with coverage
uv run pytest --cov=sql_browser
```

## Architecture

- **CLI Interface**: Typer-based command line interface with Rich formatting
- **Web Interface**: FastAPI backend + vanilla HTML/JS/CSS frontend (no build step)
- **Database Layer**: SQLite connection and schema inspection
- **Excel Support**: Workbooks converted to a temporary SQLite database (openpyxl)
- **LLM Integration**: Ollama for natural language to SQL translation, with a repair loop
- **SQL Guard**: sqlglot-based dialect normalization, read-only validation, schema checks, and row-limit enforcement
- **Value Correction**: AST-based detection/repair of guessed literal values
- **Profiling**: Per-column value/range hints cached in the database to improve accuracy
- **Output Formatting**: Rich library for beautiful terminal output
- **Logging**: Loguru for comprehensive debugging and monitoring

## Project Structure

```
sql_browser/
├── __init__.py
├── cli.py              # CLI entry point with Typer (--web launches the UI)
├── web.py              # FastAPI backend for the web UI (file/model switching)
├── static/             # Web UI frontend (index.html, app.js, style.css)
├── config.py           # Settings from .env / environment (data dir, model)
├── database.py         # Database operations and schema inspection
├── excel_loader.py     # Excel workbook -> temporary SQLite conversion
├── profiler.py         # Column value/range profiling (cached in the DB)
├── llm.py              # LLM integration, translation + repair, with logging
├── sql_guard.py        # sqlglot safety layer (transpile/validate/limit)
├── query_corrector.py  # Detect & correct guessed literal filter values
├── query_executor.py   # Query execution and Rich formatting
└── logger.py           # Logging configuration
```

## Troubleshooting

### "Failed to connect to Ollama"
- Make sure Ollama is running: `ollama serve`
- Check if the model is available: `ollama list`
- Pull the model if needed: `ollama pull gemma4:31b-cloud`

### "Database file not found"
- Verify the path to your database file
- Use absolute path if relative path doesn't work
- Create sample database: `uv run python examples/create_sample_db.py`

### JSON Parsing Errors
- Check the logs: `~/.sql_browser/logs/`
- Logs will show the exact prompt sent to LLM and response received
- Try a different model: `uv run sql-browser --model granite4.1:8b mydata.db`
- Ensure Ollama is properly configured and running

### Low Quality Results
- Try being more specific in your questions
- Use the `schema` command to understand the database structure
- Try a different model with `--model` option
- Clear conversation history with `clear` command
- Check logs for LLM confidence levels

### Import Errors
- Run `uv sync` to install all dependencies
- Make sure you're using Python 3.10 or higher

## Using Different Models

You can specify a different Ollama model:

```bash
uv run sql-browser --model granite4.1:8b sample.db
```

Available models (must be pulled first with `ollama pull <model>`):
- `gemma4:31b-cloud` (default, recommended)
- `gemma4:e4b`
- `granite4.1:8b`
- `phi4`

## Example Session

```bash
$ uv run sql-browser sample.db

SQL Browser
Connected to: sample.db

Type your questions in natural language, or:
  • 'schema' - Show database schema
  • 'tables' - List all tables
  • 'logs' - Show logs directory
  • 'help' - Show help message
  • 'exit' or 'quit' - Exit the application

Try asking:
  1. Show me all tables
  2. What are the top 5 products by price?
  3. How many customers do we have?
  4. Find all orders from the last 30 days
  5. Show me the total revenue by product category

🔍 Show me all customers from California

SQL Query - Retrieves all customers from California state
SELECT * FROM customers WHERE state = 'CA' ORDER BY name

┏━━━━┳━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━┳━━━━━━━┓
┃ id ┃ name          ┃ email              ┃ city          ┃ state ┃
┡━━━━╇━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━╇━━━━━━━┩
│ 1  │ Alice Johnson │ alice@example.com  │ San Francisco │ CA    │
│ 3  │ Carol White   │ carol@example.com  │ Los Angeles   │ CA    │
│ 9  │ Ivy Taylor    │ ivy@example.com    │ San Diego     │ CA    │
└────┴───────────────┴────────────────────┴───────────────┴───────┘

Returned 3 row(s)
```

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

MIT License