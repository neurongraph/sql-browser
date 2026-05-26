
# SQL Browser

A natural language SQLite database explorer powered by local LLM (Ollama).

## Features

- 🗣️ Natural language queries - ask questions in plain English
- 🔍 Automatic schema inspection - understands your database structure
- 🤖 Local LLM powered - uses Ollama (no API keys required)
- 📊 Rich CLI output - formatted tables and results
- ✅ Query validation and error handling
- 📝 Comprehensive logging with Loguru - debug issues easily

## Prerequisites

- Python 3.10+
- [uv](https://github.com/astral-sh/uv) - Fast Python package installer
- [Ollama](https://ollama.ai/) - Local LLM runtime

## Installation

1. Install Ollama and pull a model:
```bash
# Install Ollama from https://ollama.ai/
ollama pull llama3.2
```

2. Install the project:
```bash
uv sync
```

## Usage

```bash
# Run the SQL browser
uv run sql-browser path/to/your/database.db

# Example queries:
# - "Show me all tables"
# - "What are the columns in the users table?"
# - "Find all users created in the last month"
# - "Show me the top 10 products by price"
```

## Special Commands

While in the interactive session:

- `schema` - Display the complete database schema
- `tables` - List all tables in the database
- `clear` - Clear conversation history
- `logs` - Show logs directory location
- `help` - Show help message
- `exit` or `quit` - Exit the application

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
- **Database Layer**: SQLite connection and schema inspection
- **LLM Integration**: Ollama for natural language to SQL translation
- **Output Formatting**: Rich library for beautiful terminal output
- **Logging**: Loguru for comprehensive debugging and monitoring

## Project Structure

```
sql_browser/
├── __init__.py
├── cli.py              # CLI entry point with Typer
├── database.py         # Database operations
├── llm.py             # LLM integration with logging
├── query_executor.py  # Query execution and formatting
└── logger.py          # Logging configuration
```

## Troubleshooting

### "Failed to connect to Ollama"
- Make sure Ollama is running: `ollama serve`
- Check if the model is available: `ollama list`
- Pull the model if needed: `ollama pull llama3.2`

### "Database file not found"
- Verify the path to your database file
- Use absolute path if relative path doesn't work
- Create sample database: `uv run python examples/create_sample_db.py`

### JSON Parsing Errors
- Check the logs: `~/.sql_browser/logs/`
- Logs will show the exact prompt sent to LLM and response received
- Try a different model: `uv run sql-browser --model llama3.1 mydata.db`
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
uv run sql-browser --model llama3.1 sample.db
```

Available models (must be pulled first with `ollama pull <model>`):
- `llama3.2` (default, recommended)
- `llama3.1`
- `codellama`
- `mistral`
- `mixtral`

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