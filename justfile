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

# Launch the browser-based UI; db is optional (pick the file in the browser)
web db="" model=model port="8000":
    -lsof -ti tcp:{{port}} | xargs kill -9 2>/dev/null
    uv run sql-browser --web --port {{port}} --model "{{model}}" {{ if db != "" { '"' + db + '"' } else { "" } }}

# Generate the sample database and run sql-browser against it
run-sample model=model:
    uv run python examples/create_sample_db.py
    uv run sql-browser --model {{model}} sample.db

# Generate the sample database and open it with the example metadata file
run-sample-metadata model=model:
    uv run python examples/create_sample_db.py
    uv run sql-browser --model {{model}} --metadata examples/sample.meta.yaml sample.db

# Write a starter metadata file next to a database (add llm=1 to draft descriptions)
init-metadata db model=model llm="":
    uv run sql-browser --init-metadata {{ if llm != "" { "--llm-draft" } else { "" } }} --model {{model}} {{db}}

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
