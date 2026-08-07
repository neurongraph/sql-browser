"""FastAPI backend for the SQL Browser web UI.

A thin HTTP layer over the same core modules the CLI uses: the LLM translator,
the sqlglot guard/repair layer, the value-mismatch corrector, and the database
inspector. It is deliberately localhost-only and single-user (no auth).

The database and model are chosen in the UI rather than fixed at startup, so
the mutable state lives in a `BrowserSession` that can be re-bound at runtime
(see `/api/session`). A file passed on the command line is loaded in place and
pre-selected; anything else has to be uploaded into the configured data
directory first, because a browser can't hand the server a filesystem path.

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
from fastapi import FastAPI, File, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import DEFAULT_MODEL, SQLITE_EXTENSIONS, settings
from .context import select_examples
from .database import DatabaseInspector
from .excel_loader import EXCEL_EXTENSIONS, convert_with_metadata_detection
from .llm import LLMQueryTranslator, list_models
from .logger import get_logger
from .metadata import (
    Metadata,
    MetadataError,
    Prose,
    discover,
    load,
    load_generated_digest,
    load_prose,
    prose_digest,
    reconcile,
    regenerate_structured,
    resolve_metadata,
    write_document,
)
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

#: Everything the file selector and the upload endpoint will accept.
ALLOWED_EXTENSIONS = SQLITE_EXTENSIONS | EXCEL_EXTENSIONS

UPLOAD_CHUNK_BYTES = 1024 * 1024

NO_FILE_ERROR = "No file loaded. Upload or pick a file to begin."


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


class SessionRequest(BaseModel):
    """A file and/or model to bind. Omitted fields are left as they are."""

    file: Optional[str] = None
    model: Optional[str] = None
    #: Re-open and re-profile the file even if it's already the loaded one.
    force: bool = False


class ProsePayload(BaseModel):
    """The editable prose sections, as the frontend submits them."""

    database: Optional[str] = None
    tables: Dict[str, str] = {}
    relationships: Optional[str] = None
    examples: Optional[str] = None

    def to_prose(self) -> Prose:
        """Validate/normalize into the canonical metadata.Prose model."""
        return Prose.model_validate(self.model_dump())


class MetadataRequest(BaseModel):
    """A prose document to preview or save."""

    prose: ProsePayload = ProsePayload()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _count_items(metadata: Metadata) -> int:
    """Count the leaf facts in a metadata document.

    Used to report how much of an authored/generated document survived
    reconciliation. Relationships and examples are included; the number is a
    coarse "kept items" figure for the editor, with the warnings list carrying
    the specifics.
    """
    total = len(metadata.tables) + len(metadata.relationships) + len(metadata.examples)
    for table_meta in metadata.tables.values():
        total += len(table_meta.columns)
        for column_meta in table_meta.columns.values():
            total += len(column_meta.values)
    return total


def _prose_payload(prose: Prose) -> Dict[str, Any]:
    """Render a Prose as JSON with empty strings, friendlier for textareas."""
    return {
        "database": prose.database or "",
        "tables": dict(prose.tables),
        "relationships": prose.relationships or "",
        "examples": prose.examples or "",
    }


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


class BrowserSession:
    """The database + model the UI is currently pointed at.

    Everything mutable lives here so `/api/session` can swap the file or the
    model without restarting the server. All access is serialized with a single
    re-entrant lock: SQLite connections are opened with
    ``check_same_thread=False`` because requests are served from a thread pool,
    and a re-bind must not race a query.
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        profile: bool = True,
        metadata_path: Optional[Path] = None,
        use_metadata: bool = True,
    ):
        """Initialize an empty session (no database bound yet).

        Args:
            model: Ollama model to start with
            profile: Whether to profile a database when it's loaded, for
                column value/range hints that improve text-to-SQL accuracy
            metadata_path: An explicit metadata file for the pinned database,
                bypassing sidecar discovery
            use_metadata: Whether to look for a sidecar metadata file at all
        """
        self.lock = threading.RLock()
        self.profile_enabled = profile
        self.metadata_path = metadata_path
        self.use_metadata = use_metadata
        # Fails fast if Ollama isn't running - the whole app depends on it.
        self.llm = LLMQueryTranslator(model=model)

        self.inspector: Optional[DatabaseInspector] = None
        self.source_path: Optional[Path] = None
        self.schema_info: str = ""
        self.conversation_history: List[Dict[str, str]] = []
        self._temp_db_path: Optional[Path] = None
        #: A path given on the command line. Trusted, and loadable even though
        #: it lives outside the data directory.
        self.pinned_path: Optional[Path] = None
        #: An offer to seed metadata from an Excel workbook's own documentation
        #: sheet, set only when such a sheet was detected and no sidecar already
        #: exists. Shape: {"prose": Prose, "sheets": [titles]}. None otherwise.
        self.pending_metadata: Optional[Dict[str, Any]] = None

    # -- state ------------------------------------------------------------- #

    @property
    def ready(self) -> bool:
        """True once a database is bound and connected."""
        return self.inspector is not None

    @property
    def model(self) -> str:
        return self.llm.model

    @property
    def file_name(self) -> Optional[str]:
        """The selector value identifying the loaded file, or None."""
        return _selector_value(self.source_path) if self.source_path else None

    # -- (re)binding ------------------------------------------------------- #

    def load_file(self, path: Path) -> None:
        """Bind a SQLite database or Excel workbook, replacing any current one.

        The new database is opened and profiled *before* the old one is
        released, so a failed load leaves the previous database usable.

        Args:
            path: Path to a SQLite database or an .xlsx/.xlsm workbook

        Raises:
            FileNotFoundError: The path doesn't exist
            ExcelLoadError: The workbook couldn't be converted
        """
        with self.lock:
            temp_db_path: Optional[Path] = None
            excel_warnings: List[str] = []
            excel_metadata_text: Dict[str, str] = {}
            if path.suffix.lower() in EXCEL_EXTENSIONS:
                logger.info(f"Converting Excel workbook to temporary SQLite database: {path}")
                # Split off any sheet that is documentation rather than data, so
                # its irregular layout can't fail the whole workbook and its text
                # can seed the metadata prose. Classification degrades to "data"
                # when Ollama is unreachable.
                temp_db_path, excel_warnings, excel_metadata_text = (
                    convert_with_metadata_detection(path, classify=self.llm.classify_sheet)
                )
                resolved = temp_db_path
            else:
                resolved = path

            try:
                inspector = DatabaseInspector(str(resolved))
                inspector.connect(check_same_thread=False)

                if self.profile_enabled:
                    try:
                        inspector.profile = profile_database(inspector)
                        logger.info(f"Profiling complete for {path.name}")
                    except Exception as e:
                        logger.warning(f"Profiling failed for {path.name}: {e}")

                # Discovery keys off `path` - the workbook, not the temporary
                # database converted from it. An explicit --metadata file only
                # applies to the database it was passed alongside; a file picked
                # later in the browser gets its own sidecar or none.
                override = self.metadata_path if path == self.pinned_path else None
                inspector.metadata, inspector.metadata_warnings = resolve_metadata(
                    inspector,
                    source_path=path,
                    override=override,
                    use_file=self.use_metadata,
                )
                inspector.metadata_warnings = list(inspector.metadata_warnings) + excel_warnings

                schema_info = inspector.get_schema_description()
            except Exception:
                if temp_db_path:
                    temp_db_path.unlink(missing_ok=True)
                raise

            # Offer to convert an in-workbook documentation sheet into metadata,
            # but only when there's no sidecar already: an existing file is
            # authoritative and must never be silently overwritten.
            pending = self._build_pending_metadata(path, inspector, excel_metadata_text)

            # Only now let go of the previous database.
            self._release_database()

            self.inspector = inspector
            self.source_path = path
            self._temp_db_path = temp_db_path
            self.schema_info = schema_info
            self.pending_metadata = pending
            # Past turns described a different database; they'd mislead the LLM.
            self.conversation_history = []
            logger.info(f"Loaded {path} (model: {self.model})")

    @staticmethod
    def _build_pending_metadata(
        source_path: Path,
        inspector: DatabaseInspector,
        metadata_text: Dict[str, str],
    ) -> Optional[Dict[str, Any]]:
        """Turn detected documentation-sheet text into a pending prose offer.

        Returns None (no offer) when there was no documentation sheet or a
        sidecar already exists for this source.
        """
        if not metadata_text or discover(source_path) is not None:
            return None

        combined = "\n\n".join(metadata_text.values())
        real_tables = inspector.get_tables()
        if len(real_tables) == 1:
            prose = Prose(tables={real_tables[0]: combined})
        else:
            prose = Prose(database=combined)
        return {"prose": prose, "sheets": list(metadata_text.keys())}

    def set_model(self, model: str) -> None:
        """Point the translator at a different Ollama model.

        Cheap, and independent of the database: the schema and the conversation
        transcript are left alone.
        """
        with self.lock:
            if model != self.llm.model:
                logger.info(f"Switching model: {self.llm.model} -> {model}")
                self.llm.model = model

    def close(self) -> None:
        """Release the database connection and any temporary file."""
        with self.lock:
            self._release_database()
            self.inspector = None
            self.source_path = None
            self.schema_info = ""
            self.pending_metadata = None

    # -- metadata editing -------------------------------------------------- #

    def sidecar_path(self) -> Optional[Path]:
        """Where this source's metadata sidecar lives, existing or to-be-written.

        An existing sidecar (however named) is reused; otherwise the canonical
        ``<stem>.meta.yaml`` next to the source/workbook - never next to the
        throwaway temp database.
        """
        if not self.source_path:
            return None
        existing = discover(self.source_path)
        if existing is not None:
            return existing
        return self.source_path.with_name(f"{self.source_path.stem}.meta.yaml")

    def _existing_structured(self) -> Metadata:
        """Load the current structured blocks, for section preservation on Save."""
        path = discover(self.source_path) if self.source_path else None
        if path is None:
            return Metadata()
        try:
            return load(path)
        except MetadataError:
            return Metadata()

    def reload_metadata(self) -> List[str]:
        """Re-resolve metadata and rebuild the schema text after a write."""
        with self.lock:
            override = (
                self.metadata_path if self.source_path == self.pinned_path else None
            )
            self.inspector.metadata, self.inspector.metadata_warnings = resolve_metadata(
                self.inspector,
                source_path=self.source_path,
                override=override,
                use_file=self.use_metadata,
            )
            self.schema_info = self.inspector.get_schema_description()
            return list(self.inspector.metadata_warnings)

    def preview_metadata(self, prose: Prose) -> Dict[str, Any]:
        """Dry-run: regenerate structured blocks and reconcile, writing nothing."""
        with self.lock:
            raw, gen_warnings = regenerate_structured(
                prose, self.llm, self.inspector, existing=self._existing_structured()
            )
            reconciled, recon_warnings = reconcile(raw, self.inspector)
        return {
            "matched": _count_items(reconciled),
            "dropped": len(recon_warnings),
            "warnings": gen_warnings + recon_warnings,
            "prose": _prose_payload(prose),
        }

    def save_metadata(self, prose: Prose) -> Dict[str, Any]:
        """Regenerate, reconcile, write the sidecar, reload, and clear any offer.

        Degrades rather than failing: if the model is unreachable the prose is
        still written (with warnings), because prose is the canonical artifact.
        """
        with self.lock:
            existing = self._existing_structured()
            raw, gen_warnings = regenerate_structured(
                prose, self.llm, self.inspector, existing=existing
            )
            reconciled, recon_warnings = reconcile(raw, self.inspector)
            dest = self.sidecar_path()
            write_document(
                dest, prose, raw, source_name=self.source_path.name, model=self.model
            )
            self.reload_metadata()
            self.pending_metadata = None
        return {
            "ok": True,
            "path": str(dest),
            "matched": _count_items(reconciled),
            "dropped": len(recon_warnings),
            "warnings": gen_warnings + recon_warnings,
            "prose": _prose_payload(prose),
        }

    def _release_database(self) -> None:
        """Disconnect the current inspector and delete its temp database."""
        if self.inspector:
            self.inspector.disconnect()
        if self._temp_db_path:
            logger.debug(f"Removing temporary database: {self._temp_db_path}")
            self._temp_db_path.unlink(missing_ok=True)
            self._temp_db_path = None

    # -- query pipeline ---------------------------------------------------- #

    def execute(self, sql: str) -> Dict[str, Any]:
        """Run a read-only query and return columns + JSON-safe rows."""
        with self.lock:
            column_names, rows = self.inspector.execute_query(sql)
        return {
            "sql": sql,
            "columns": column_names,
            "rows": _rows_to_json(column_names, rows),
            "row_count": len(rows),
        }

    def guard_and_repair(
        self, user_query: str, sql: str, explanation: str, schema_info: str
    ) -> Dict[str, Any]:
        """Normalize, validate, and (once) repair a generated query.

        Args:
            user_query: The user's original question, for the repair prompt
            sql: The LLM-generated SQL to validate
            explanation: The LLM's explanation, kept unless repair replaces it
            schema_info: The (possibly pruned) schema text this query was
                generated against - the repair prompt reuses it rather than
                the session's unpruned text, so the model sees the same
                tables it originally reasoned about

        Returns a dict with either an "error" key or a validated "sql" +
        "explanation".
        """
        sql = transpile_to_sqlite(sql)
        if not is_read_only(sql):
            return {"error": "Only read-only SELECT queries are allowed."}

        with self.lock:
            schema = build_schema(self.inspector)
        problems = find_schema_problems(sql, schema)
        if problems:
            logger.info(f"Schema problems, attempting repair: {problems}")
            try:
                repaired = self.llm.repair_sql(
                    user_query,
                    schema_info,
                    sql,
                    "; ".join(problems),
                    self.conversation_history,
                )
                candidate = transpile_to_sqlite(repaired["sql"])
                if is_read_only(candidate):
                    sql = candidate
                    explanation = repaired.get("explanation", explanation)
            except Exception as e:
                logger.warning(f"Repair attempt failed: {e}")

        return {"sql": sql, "explanation": explanation}

    def remember(self, user_query: str, sql: str, explanation: str) -> None:
        """Append a turn to the conversation history, keeping it bounded."""
        self.conversation_history.append({"role": "user", "content": user_query})
        self.conversation_history.append(
            {"role": "assistant", "content": f"SQL: {sql}\nExplanation: {explanation}"}
        )
        if len(self.conversation_history) > 20:
            del self.conversation_history[:-20]


# --------------------------------------------------------------------------- #
# File selection
# --------------------------------------------------------------------------- #

def _selector_value(path: Path) -> str:
    """The string the UI uses to refer to a file.

    Files inside the data directory are identified by name; a file pinned from
    the command line keeps its full path so it stays distinguishable.
    """
    data_dir = settings.resolved_data_dir()
    resolved = path.expanduser().resolve()
    if resolved.parent == data_dir:
        return resolved.name
    return str(resolved)


def _list_data_files() -> List[str]:
    """Names of loadable files sitting in the data directory, sorted."""
    data_dir = settings.resolved_data_dir()
    names = [
        entry.name
        for entry in data_dir.iterdir()
        if entry.is_file() and entry.suffix.lower() in ALLOWED_EXTENSIONS
    ]
    return sorted(names, key=str.lower)


def _resolve_selection(value: str, pinned: Optional[Path]) -> Path:
    """Turn a selector value from the UI into a path that's safe to open.

    Only two things are loadable: a file inside the data directory, and the
    path that was pinned on the command line. Anything else - in particular a
    name containing `..` or an absolute path - is rejected, so a request can't
    reach arbitrary files on disk.

    Raises:
        ValueError: The value escapes the data directory and isn't the pinned path
        FileNotFoundError: The resolved path isn't a file
    """
    data_dir = settings.resolved_data_dir()
    candidate = Path(value).expanduser()

    if pinned and candidate.resolve() == pinned.expanduser().resolve():
        resolved = pinned.expanduser().resolve()
    else:
        resolved = (data_dir / candidate.name).resolve()
        if resolved.parent != data_dir or candidate.name != value:
            raise ValueError(
                f"'{value}' is not in the data directory. "
                "Upload it, or pass it on the command line."
            )

    if resolved.suffix.lower() not in ALLOWED_EXTENSIONS:
        raise ValueError(f"Unsupported file type: '{resolved.suffix}'")
    if not resolved.is_file():
        raise FileNotFoundError(f"File not found: {value}")
    return resolved


def _unique_upload_path(data_dir: Path, filename: str) -> Path:
    """Pick a non-clobbering destination for an upload.

    `report.xlsx` becomes `report-1.xlsx` when the name is already taken.
    """
    base = Path(filename).stem
    suffix = Path(filename).suffix
    destination = data_dir / f"{base}{suffix}"
    counter = 1
    while destination.exists():
        destination = data_dir / f"{base}-{counter}{suffix}"
        counter += 1
    return destination


# --------------------------------------------------------------------------- #
# Application
# --------------------------------------------------------------------------- #

def create_app(
    db_path: Optional[str] = None,
    model: Optional[str] = None,
    profile: bool = True,
    metadata_path: Optional[Path] = None,
    use_metadata: bool = True,
) -> FastAPI:
    """Build the FastAPI app.

    Args:
        db_path: Optional path to a SQLite database or Excel workbook to load
            at startup and pre-select in the UI. Read in place, never copied.
        model: Ollama model to start with (defaults to the configured one)
        profile: Whether to profile a database when it's loaded, for value hints
        metadata_path: An explicit metadata file for `db_path`, bypassing
            sidecar discovery
        use_metadata: Whether to look for a sidecar metadata file at all

    Returns:
        A configured FastAPI application.
    """
    session = BrowserSession(
        model=model or DEFAULT_MODEL,
        profile=profile,
        metadata_path=metadata_path,
        use_metadata=use_metadata,
    )

    if db_path:
        source = Path(db_path).expanduser().resolve()
        session.pinned_path = source
        session.load_file(source)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        logger.debug("Shutting down web app; closing database")
        session.close()

    app = FastAPI(title="SQL Browser", lifespan=lifespan)

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> FileResponse:
        # index.html points at the SVG directly; this covers browsers that
        # probe the well-known path anyway, so it doesn't log a 404.
        return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")

    # -- session / selectors ------------------------------------------------ #

    def _config_payload() -> Dict[str, Any]:
        files: List[Dict[str, str]] = [
            {"value": name, "label": name} for name in _list_data_files()
        ]
        # A file pinned on the command line lives outside the data directory,
        # so add it explicitly to keep it selectable. Its value is the full
        # path (to stay unambiguous) but it's labelled by name.
        if session.pinned_path:
            pinned = _selector_value(session.pinned_path)
            if all(entry["value"] != pinned for entry in files):
                files.insert(
                    0,
                    {
                        "value": pinned,
                        "label": f"{session.pinned_path.name}  (command line)",
                    },
                )

        models = list_models()
        if session.model not in models:
            models.insert(0, session.model)

        return {
            "file": session.file_name,
            "database": session.source_path.name if session.source_path else None,
            "model": session.model,
            "files": files,
            "models": models,
            "ready": session.ready,
            "data_dir": str(settings.resolved_data_dir()),
            "max_upload_mb": settings.max_upload_mb,
            # A workbook whose documentation sheet can be turned into metadata.
            "metadata_suggestion": session.pending_metadata is not None,
            "metadata_editable": session.ready,
        }

    @app.get("/api/config")
    def get_config() -> Dict[str, Any]:
        return _config_payload()

    @app.post("/api/session")
    def set_session(request: SessionRequest) -> Dict[str, Any]:
        """Bind a file and/or a model, then report the new configuration."""
        if request.model:
            session.set_model(request.model)

        file_changed = False
        if request.file and (request.force or request.file != session.file_name):
            try:
                path = _resolve_selection(request.file, session.pinned_path)
                session.load_file(path)
                file_changed = True
            except (ValueError, FileNotFoundError) as e:
                logger.warning(f"Rejected file selection '{request.file}': {e}")
                return {"error": str(e), **_config_payload()}
            except Exception as e:
                logger.exception(f"Failed to load '{request.file}'")
                issues = getattr(e, "issues", None)
                detail = "; ".join(issues) if issues else str(e)
                return {"error": f"Could not load '{request.file}': {detail}",
                        **_config_payload()}

        return {"ok": True, "file_changed": file_changed, **_config_payload()}

    @app.post("/api/upload")
    async def upload(file: UploadFile = File(...)) -> Dict[str, Any]:
        """Store an uploaded database/workbook in the data directory.

        Only the base name of the client-supplied filename is used, so a
        crafted name can't write outside the data directory.
        """
        name = Path(file.filename or "").name
        if not name:
            return {"error": "No filename provided."}
        if Path(name).suffix.lower() not in ALLOWED_EXTENSIONS:
            allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
            return {"error": f"Unsupported file type. Allowed: {allowed}"}

        data_dir = settings.resolved_data_dir()
        destination = _unique_upload_path(data_dir, name)
        limit = settings.max_upload_mb * 1024 * 1024
        written = 0

        try:
            with destination.open("wb") as out:
                while chunk := await file.read(UPLOAD_CHUNK_BYTES):
                    written += len(chunk)
                    if written > limit:
                        raise ValueError(
                            f"File is larger than the {settings.max_upload_mb} MB limit."
                        )
                    out.write(chunk)
        except Exception as e:
            destination.unlink(missing_ok=True)
            logger.warning(f"Upload of '{name}' failed: {e}")
            return {"error": str(e)}
        finally:
            await file.close()

        logger.info(f"Uploaded {destination} ({written} bytes)")
        # `uploaded` is the (possibly de-duplicated) stored name for the UI to
        # select; the rest is the refreshed selector state.
        return {"uploaded": destination.name, **_config_payload()}

    # -- data --------------------------------------------------------------- #

    @app.get("/api/schema")
    def get_schema() -> Dict[str, Any]:
        if not session.ready:
            return {"error": NO_FILE_ERROR}
        with session.lock:
            full_schema = session.inspector.get_full_schema()
            profile_data = session.inspector.profile
            metadata = session.inspector.metadata
            metadata_warnings = list(session.inspector.metadata_warnings)
        tables = []
        for name, info in full_schema.items():
            table_meta = metadata.table(name) if metadata else None
            tables.append({
                "name": name,
                "row_count": info["row_count"],
                "description": table_meta.description if table_meta else None,
                "grain": table_meta.grain if table_meta else None,
                "columns": [
                    {
                        "name": c["name"],
                        "type": c["type"],
                        "pk": c["pk"],
                        "description": (
                            metadata.column(name, c["name"]).description
                            if metadata
                            else None
                        ),
                    }
                    for c in info["columns"]
                ],
                "profile": profile_data.get(name, {}),
            })
        return {
            "database": session.source_path.name,
            "tables": tables,
            "relationships": [
                {
                    "from": r.from_,
                    "to": r.to,
                    "label": r.describe(),
                    "composite": r.is_composite,
                    "declared": r.declared,
                }
                for r in (metadata.relationships if metadata else [])
            ],
            "metadata_warnings": metadata_warnings,
        }

    # -- metadata editing --------------------------------------------------- #
    #
    # These bind to the RAW document (prose + pre-reconcile structured blocks),
    # never the reconciled projection that /api/schema serves. Editing the
    # reconciled view would silently drop mid-edit entries and write the
    # auto-merged declared foreign keys back into the file. All four degrade to
    # warnings rather than 500, so losing the LLM never breaks the editor.

    @app.get("/api/metadata")
    def get_metadata() -> Dict[str, Any]:
        if not session.ready:
            return {"error": NO_FILE_ERROR}
        with session.lock:
            inspector = session.inspector
            real_tables = inspector.get_tables()
            sidecar = discover(session.source_path)
            exists = sidecar is not None
            pending = session.pending_metadata

            if pending is not None:
                prose = pending["prose"]
            elif exists:
                prose = load_prose(sidecar)
            else:
                prose = Prose()

            raw = load(sidecar) if exists else Metadata()
            reconciled, recon_warnings = reconcile(raw, inspector)

            file_prose = load_prose(sidecar) if exists else Prose()
            stale = bool(
                exists
                and not file_prose.is_empty
                and load_generated_digest(sidecar) != prose_digest(file_prose)
            )

        return {
            "exists": exists,
            "path": str(sidecar) if exists else None,
            "prose": _prose_payload(prose),
            "tables": real_tables,
            "structured_present": not raw.is_empty,
            "stale": stale,
            "pending": {"sheets": pending["sheets"]} if pending else None,
            "reconcile": {
                "matched": _count_items(reconciled),
                "dropped": len(recon_warnings),
                "warnings": recon_warnings,
            },
        }

    @app.post("/api/metadata/preview")
    def preview_metadata(request: MetadataRequest) -> Dict[str, Any]:
        if not session.ready:
            return {"error": NO_FILE_ERROR}
        try:
            return session.preview_metadata(request.prose.to_prose())
        except Exception as e:
            logger.exception("Metadata preview failed")
            return {"error": f"Preview failed: {e}"}

    @app.post("/api/metadata")
    def save_metadata(request: MetadataRequest) -> Dict[str, Any]:
        if not session.ready:
            return {"error": NO_FILE_ERROR}
        try:
            return session.save_metadata(request.prose.to_prose())
        except Exception as e:
            logger.exception("Metadata save failed")
            return {"error": f"Save failed: {e}"}

    @app.delete("/api/metadata/pending")
    def discard_pending_metadata() -> Dict[str, Any]:
        with session.lock:
            session.pending_metadata = None
        return {"ok": True}

    @app.get("/api/suggestions")
    def get_suggestions() -> Dict[str, Any]:
        if not session.ready:
            return {"suggestions": []}
        try:
            suggestions = session.llm.get_query_suggestions(session.schema_info)
        except Exception as e:
            logger.warning(f"Suggestions failed: {e}")
            suggestions = []
        return {"suggestions": suggestions}

    @app.post("/api/query")
    def run_query(request: QueryRequest) -> Dict[str, Any]:
        if not session.ready:
            return {"error": NO_FILE_ERROR}

        query = request.query.strip()
        if not query:
            return {"error": "Empty query."}

        logger.info(f"Web query: {query}")

        # Pruned to the tables this question scores as relevant, with the
        # curated examples that best match it - both computed per question,
        # not at load time. session.schema_info (unpruned) is left for
        # /api/schema and /api/suggestions, which have no question to prune
        # against.
        with session.lock:
            query_schema_info = session.inspector.get_schema_description(question=query)
            query_examples = select_examples(
                query,
                session.inspector.metadata.examples if session.inspector.metadata else [],
            )

        try:
            result = session.llm.translate_to_sql(
                query, query_schema_info, session.conversation_history, examples=query_examples
            )
        except Exception as e:
            logger.error(f"Translation failed: {e}")
            return {"error": f"Translation error: {e}"}

        guarded = session.guard_and_repair(
            query, result["sql"], result["explanation"], query_schema_info
        )
        if "error" in guarded:
            return {"sql": result["sql"], **guarded}

        sql = guarded["sql"]
        explanation = guarded["explanation"]
        confidence = result.get("confidence", "medium")

        # Auto-fix obvious value typos; surface ambiguous ones to the picker.
        with session.lock:
            fixed_sql, mismatches = find_value_mismatches(sql, session.inspector)

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
                execution = session.execute(final_sql)
            except Exception as e:
                logger.warning(f"Query execution failed: {e}")
                response["result"] = {"error": str(e)}
                return response
            response["result"] = execution
            session.remember(query, final_sql, explanation)
        else:
            response["result"] = None

        return response

    @app.post("/api/run-sql")
    def run_sql(request: RunSqlRequest) -> Dict[str, Any]:
        if not session.ready:
            return {"error": NO_FILE_ERROR}

        choices = [c.model_dump() for c in request.choices]
        sql = apply_value_choices(request.sql, choices)
        sql = transpile_to_sqlite(sql)

        if not is_read_only(sql):
            return {"error": "Only read-only SELECT queries are allowed."}

        sql = enforce_limit(sql)
        try:
            return session.execute(sql)
        except Exception as e:
            logger.warning(f"run-sql execution failed: {e}")
            return {"error": str(e), "sql": sql}

    # Serve the frontend assets (index.html is served explicitly at "/").
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    return app


def run_web(
    db_path: Optional[str] = None,
    model: Optional[str] = None,
    profile: bool = True,
    host: str = "127.0.0.1",
    port: int = 8000,
    metadata_path: Optional[Path] = None,
    use_metadata: bool = True,
) -> None:
    """Create the app and serve it with uvicorn (blocking)."""
    app = create_app(
        db_path,
        model=model,
        profile=profile,
        metadata_path=metadata_path,
        use_metadata=use_metadata,
    )
    print(f"\n  SQL Browser web UI running at  http://{host}:{port}\n  Press Ctrl+C to stop.\n")
    uvicorn.run(app, host=host, port=port, log_level="info")

# Made with Bob
