model := "gemma4:31b-cloud"

# List available recipes
default:
    @just --list

# Install/sync project dependencies
install:
    uv sync

# Run sql-browser against a SQLite db or Excel workbook
run db model=model:
    uv run sql-browser --model {{model}} {{db}}

# Run sql-browser without profiling the database first
run-no-profile db model=model:
    uv run sql-browser --model {{model}} --no-profile {{db}}

# Launch the browser-based UI against a SQLite db or Excel workbook
web db model=model port="8000":
    uv run sql-browser --web --port {{port}} --model "{{model}}" "{{db}}"

# Generate the sample database and run sql-browser against it
run-sample model=model:
    uv run python examples/create_sample_db.py
    uv run sql-browser --model {{model}} sample.db

# Run the test suite
test:
    uv run pytest

# Run the test suite with coverage
test-cov:
    uv run pytest --cov=sql_browser

# Remove caches and build artifacts
clean:
    find . -type d -name __pycache__ -not -path "./.venv/*" -exec rm -rf {} +
    rm -rf .pytest_cache dist build *.egg-info
