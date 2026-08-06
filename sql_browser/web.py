"""FastAPI backend for the SQL Browser web UI.

A thin HTTP layer over the same core modules the CLI uses: the LLM translator,
the sqlglot guard/repair layer, the value-mismatch corrector, and the database
inspector. It is deliberately localhost-only and single-user (no auth, no file
uploads) - it serves the static frontend and a small JSON API.

The value-correction step can't block on a terminal prompt over HTTP, so it is
split into two phases:

  POST /api/query   translate NL -> SQL, guard/repair, auto-fix obvious value
                    typos. If ambiguous values remain, return them as
                    `mismatches` for the UI's picker instead of executing.
  POST /api/run-sql apply the user's value picks (if any), re-validate the
                    (possibly hand-edited) SQL as read-only, and execute.
"""

import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .database import DatabaseInspector
from .excel_loader import EXCEL_EXTENSIONS, convert_xlsx_to_sqlite
from .llm import LLMQueryTranslator
from .logger import get_logger
from .profiler import profile_database
from .query_corrector import apply_value_choices, find_value_mismatches
from .sql_guard import (
    build_schema,
    enforce_limit,
    find_schema_problems,
    is_read_only,
    transpile_to_sqlite,
)

logger = get_logger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #

class QueryRequest(BaseModel):
    query: str


class ValueChoice(BaseModel):
    column: str
    attempted_value: str
    chosen: Optional[str] = None


class RunSqlRequest(BaseModel):
    sql: str
    choices: List[ValueChoice] = []


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _rows_to_json(column_names: List[str], rows: List[Any]) -> List[List[Any]]:
    """Convert result rows to JSON-safe lists of values."""
    safe_rows = []
    for row in rows:
        safe_row = []
        for value in row:
            if isinstance(value, bytes):
                safe_row.append(value.decode("utf-8", "replace"))
            else:
                safe_row.append(value)
        safe_rows.append(safe_row)
    return safe_rows


def create_app(db_path: str, model: str = "gemma4:31b-cloud", profile: bool = True) -> FastAPI:
    """Build the FastAPI app wired to a specific database.

    Args:
        db_path: Path to a SQLite database or an Excel workbook
        model: Ollama model to use
        profile: Whether to profile the database at startup for value hints

    Returns:
        A configured FastAPI application.
    """
    source_path = Path(db_path)
    temp_db_path: Optional[Path] = None

    if source_path.suffix.lower() in EXCEL_EXTENSIONS:
        logger.info(f"Converting Excel workbook to temporary SQLite database: {source_path}")
        temp_db_path = convert_xlsx_to_sqlite(source_path)
        resolved_db_path = str(temp_db_path)
    else:
        resolved_db_path = str(source_path)

    inspector = DatabaseInspector(resolved_db_path)
    # Web requests are served from a thread pool; connect accordingly and
    # serialize all database access with a lock.
    inspector.connect(check_same_thread=False)
    lock = threading.Lock()

    llm = LLMQueryTranslator(model=model)

    if profile:
        try:
            inspector.profile = profile_database(inspector)
            logger.info("Startup profiling complete")
        except Exception as e:
            logger.warning(f"Startup profiling failed: {e}")

    schema_info = inspector.get_schema_description()
    conversation_history: List[Dict[str, str]] = []

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        logger.debug("Shutting down web app; closing database")
        inspector.disconnect()
        if temp_db_path:
            temp_db_path.unlink(missing_ok=True)

    app = FastAPI(title="SQL Browser", lifespan=lifespan)

    def _execute(sql: str) -> Dict[str, Any]:
        """Run a read-only query and return columns + JSON-safe rows."""
        with lock:
            column_names, rows = inspector.execute_query(sql)
        return {
            "sql": sql,
            "columns": column_names,
            "rows": _rows_to_json(column_names, rows),
            "row_count": len(rows),
        }

    def _guard_and_repair(user_query: str, sql: str, explanation: str) -> Dict[str, Any]:
        """Normalize, validate, and (once) repair a generated query.

        Returns a dict with either an "error" key or a validated "sql" +
        "explanation".
        """
        sql = transpile_to_sqlite(sql)
        if not is_read_only(sql):
            return {"error": "Only read-only SELECT queries are allowed."}

        with lock:
            schema = build_schema(inspector)
        problems = find_schema_problems(sql, schema)
        if problems:
            logger.info(f"Schema problems, attempting repair: {problems}")
            try:
                repaired = llm.repair_sql(
                    user_query, schema_info, sql, "; ".join(problems), conversation_history
                )
                candidate = transpile_to_sqlite(repaired["sql"])
                if is_read_only(candidate):
                    sql = candidate
                    explanation = repaired.get("explanation", explanation)
            except Exception as e:
                logger.warning(f"Repair attempt failed: {e}")

        return {"sql": sql, "explanation": explanation}

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/schema")
    def get_schema() -> Dict[str, Any]:
        with lock:
            full_schema = inspector.get_full_schema()
            profile_data = inspector.profile
        tables = []
        for name, info in full_schema.items():
            tables.append({
                "name": name,
                "row_count": info["row_count"],
                "columns": [
                    {"name": c["name"], "type": c["type"], "pk": c["pk"]}
                    for c in info["columns"]
                ],
                "profile": profile_data.get(name, {}),
            })
        return {"database": source_path.name, "tables": tables}

    @app.get("/api/suggestions")
    def get_suggestions() -> Dict[str, Any]:
        try:
            suggestions = llm.get_query_suggestions(schema_info)
        except Exception as e:
            logger.warning(f"Suggestions failed: {e}")
            suggestions = []
        return {"suggestions": suggestions}

    @app.post("/api/query")
    def run_query(request: QueryRequest) -> Dict[str, Any]:
        query = request.query.strip()
        if not query:
            return {"error": "Empty query."}

        logger.info(f"Web query: {query}")
        try:
            result = llm.translate_to_sql(query, schema_info, conversation_history)
        except Exception as e:
            logger.error(f"Translation failed: {e}")
            return {"error": f"Translation error: {e}"}

        guarded = _guard_and_repair(query, result["sql"], result["explanation"])
        if "error" in guarded:
            return {"sql": result["sql"], **guarded}

        sql = guarded["sql"]
        explanation = guarded["explanation"]
        confidence = result.get("confidence", "medium")

        # Auto-fix obvious value typos; surface ambiguous ones to the picker.
        with lock:
            fixed_sql, mismatches = find_value_mismatches(sql, inspector)

        response: Dict[str, Any] = {
            "sql": fixed_sql,
            "explanation": explanation,
            "confidence": confidence,
            "mismatches": mismatches,
        }

        # If there's nothing to disambiguate, run it now for a snappy result.
        if not mismatches:
            final_sql = enforce_limit(fixed_sql)
            try:
                execution = _execute(final_sql)
            except Exception as e:
                logger.warning(f"Query execution failed: {e}")
                response["result"] = {"error": str(e)}
                return response
            response["result"] = execution
            _remember(query, final_sql, explanation)
        else:
            response["result"] = None

        return response

    @app.post("/api/run-sql")
    def run_sql(request: RunSqlRequest) -> Dict[str, Any]:
        choices = [c.model_dump() for c in request.choices]
        sql = apply_value_choices(request.sql, choices)
        sql = transpile_to_sqlite(sql)

        if not is_read_only(sql):
            return {"error": "Only read-only SELECT queries are allowed."}

        sql = enforce_limit(sql)
        try:
            return _execute(sql)
        except Exception as e:
            logger.warning(f"run-sql execution failed: {e}")
            return {"error": str(e), "sql": sql}

    def _remember(user_query: str, sql: str, explanation: str) -> None:
        conversation_history.append({"role": "user", "content": user_query})
        conversation_history.append(
            {"role": "assistant", "content": f"SQL: {sql}\nExplanation: {explanation}"}
        )
        if len(conversation_history) > 20:
            del conversation_history[:-20]

    # Serve the frontend assets (index.html is served explicitly at "/").
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    return app


def run_web(
    db_path: str,
    model: str = "gemma4:31b-cloud",
    profile: bool = True,
    host: str = "127.0.0.1",
    port: int = 8000,
) -> None:
    """Create the app and serve it with uvicorn (blocking)."""
    app = create_app(db_path, model=model, profile=profile)
    print(f"\n  SQL Browser web UI running at  http://{host}:{port}\n  Press Ctrl+C to stop.\n")
    uvicorn.run(app, host=host, port=port, log_level="info")

# Made with Bob
