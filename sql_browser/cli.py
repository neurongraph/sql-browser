"""Command-line interface for SQL Browser."""

import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import typer
from rich.prompt import Prompt

from .config import DEFAULT_MODEL, settings
from .context import select_examples
from .database import (
    DatabaseInspector,
    DuckDBInspector,
    SQLiteInspector,
    create_inspector,
)
from .excel_loader import EXCEL_EXTENSIONS, ExcelLoadError, convert_xlsx_to_sqlite
from .llm import LLMQueryTranslator
from .metadata import (
    MetadataError,
    Metadata,
    build_scaffold,
    discover,
    load,
    load_prose,
    reconcile,
    regenerate_structured,
    resolve_metadata,
    write_document,
)
from .profiler import profile_database
from .query_corrector import find_value_mismatches, resolve_value_mismatches
from .query_executor import QueryExecutor
from .sql_guard import (
    build_schema,
    enforce_limit,
    find_schema_problems,
    is_read_only,
    transpile_to_dialect,
    transpile_to_sqlite,
)
from .logger import get_logger, LOGS_DIR

logger = get_logger(__name__)


class SQLBrowserSession:
    """Manages an interactive SQL Browser session."""

    def __init__(
        self,
        db_path: str,
        model: str = DEFAULT_MODEL,
        profile: bool = True,
        metadata_path: Optional[Path] = None,
        use_metadata: bool = True,
    ):
        """Initialize the session.

        Args:
            db_path: Path to the SQLite database, or an Excel workbook
                (each sheet becomes a table in a temporary SQLite database)
            model: Ollama model to use
            profile: Whether to profile the database (column value/range
                hints) at startup to improve text-to-SQL accuracy
            metadata_path: Explicit metadata file, bypassing sidecar discovery
            use_metadata: Whether to look for a sidecar metadata file at all
        """
        self.source_path = Path(db_path)
        self.metadata_path = metadata_path
        self.use_metadata = use_metadata
        self._temp_db_path: Optional[Path] = None

        if self.source_path.suffix.lower() in EXCEL_EXTENSIONS:
            logger.info(f"Converting Excel workbook to temporary SQLite database: {self.source_path}")
            self._temp_db_path = convert_xlsx_to_sqlite(self.source_path)
            self.db_path = str(self._temp_db_path)
        else:
            self.db_path = str(self.source_path)

        self.db_inspector = DatabaseInspector(self.db_path)
        self.llm = LLMQueryTranslator(model=model)
        self.executor: Optional[QueryExecutor] = None
        self.conversation_history = []
        self.profile_enabled = profile
        #: The rendered schema/metadata text sent to the LLM. Held on the session
        #: rather than passed down the call stack so the `metadata` and `profile`
        #: commands can rebuild it mid-session.
        self.schema_info = ""

    def start(self) -> None:
        """Start the interactive session."""
        logger.info(f"Starting SQL Browser session with database: {self.db_path}")
        logger.info(f"Using LLM model: {self.llm.model}")
        
        try:
            # Connect to database
            logger.debug("Connecting to database...")
            self.db_inspector.connect()
            self.executor = QueryExecutor(self.db_inspector)
            logger.debug("Database connected successfully")
            
            # Display welcome message
            self.executor.display_welcome(str(self.source_path))

            if self.profile_enabled:
                self._run_profiling()

            # After profiling, so a `values:` block can be checked against the
            # values the profiler already found, and before the schema text is
            # built, since that text is what carries metadata to the model.
            self._load_metadata()

            # Get schema for LLM context
            self.schema_info = self.db_inspector.get_schema_description()

            # Show suggestions
            with self.executor.console.status(
                "[cyan]Waiting for LLM to generate suggestions...[/cyan]", spinner="dots"
            ):
                suggestions = self.llm.get_query_suggestions(self.schema_info)
            self.executor.display_suggestions(suggestions)
            
            # Main interaction loop
            self._interaction_loop()
            
        except typer.Exit:
            # `typer.Exit` subclasses RuntimeError, so it has to be re-raised
            # before the handler below turns a clean `exit` into "Error: 0".
            raise
        except FileNotFoundError as e:
            logger.error(f"Database file not found: {e}")
            typer.echo(f"Error: {e}", err=True)
            raise typer.Exit(1)
        except RuntimeError as e:
            logger.error(f"Runtime error: {e}")
            typer.echo(f"Error: {e}", err=True)
            raise typer.Exit(1)
        except KeyboardInterrupt:
            logger.info("User interrupted session")
            typer.echo("\n\nGoodbye!")
        finally:
            if self.db_inspector:
                logger.debug("Disconnecting from database")
                self.db_inspector.disconnect()
            if self._temp_db_path:
                logger.debug(f"Removing temporary database: {self._temp_db_path}")
                self._temp_db_path.unlink(missing_ok=True)

    def _run_profiling(self, force: bool = False) -> None:
        """Profile the database and report a short summary."""
        logger.debug("Profiling database...")
        self.executor.display_info("Profiling database...")
        self.db_inspector.profile = profile_database(self.db_inspector, force=force)

        enum_cols = sum(
            1 for cols in self.db_inspector.profile.values()
            for info in cols.values() if info["type"] == "enum"
        )
        range_cols = sum(
            1 for cols in self.db_inspector.profile.values()
            for info in cols.values() if info["type"] == "range"
        )
        logger.info(f"Profiling complete: {enum_cols} enum column(s), {range_cols} range column(s)")
        self.executor.display_info(
            f"Profiling complete: {enum_cols} categorical column(s), {range_cols} numeric range column(s)"
        )

    def _load_metadata(self, rebuild_prompt: bool = False) -> None:
        """Load (or reload) the curated metadata layer and report what came of it.

        Args:
            rebuild_prompt: Re-render the schema text afterwards. Needed when
                reloading mid-session, since the loaded metadata only reaches the
                model through that text.
        """
        self.db_inspector.metadata, self.db_inspector.metadata_warnings = resolve_metadata(
            self.db_inspector,
            source_path=self.source_path,
            override=self.metadata_path,
            use_file=self.use_metadata,
        )
        metadata = self.db_inspector.metadata
        warnings = self.db_inspector.metadata_warnings

        documented = sum(1 for t in metadata.tables.values() if t.description)
        if not metadata.is_empty:
            self.executor.display_info(
                f"Metadata loaded: {documented} documented table(s), "
                f"{len(metadata.relationships)} relationship(s)"
            )
        if warnings:
            self.executor.display_warning(
                f"{len(warnings)} metadata warning(s) - type 'metadata' to see them"
            )

        if rebuild_prompt:
            self.schema_info = self.db_inspector.get_schema_description()

    def _query_context(self, user_input: str) -> Tuple[str, List[Dict[str, str]]]:
        """Build the per-question schema text and curated examples for one turn.

        Computed fresh each turn rather than reusing `self.schema_info`,
        because relevance (context.py) depends on what's actually being
        asked - `self.schema_info` stays unpruned for the `schema` command and
        the startup suggestions, which have no question to prune against.

        Returns:
            (schema text pruned to this question, examples that best match it)
        """
        schema_info = self.db_inspector.get_schema_description(question=user_input)
        examples = select_examples(
            user_input,
            self.db_inspector.metadata.examples if self.db_inspector.metadata else [],
        )
        return schema_info, examples

    def _interaction_loop(self) -> None:
        """Main interaction loop.

        Reads `self.schema_info` each turn rather than taking it as a parameter,
        so the `metadata` command can reload the file from disk and have the very
        next question use the rebuilt prompt.
        """
        while True:
            try:
                # Get user input
                user_input = Prompt.ask("\n🔍")
                
                if not user_input.strip():
                    continue
                
                logger.info(f"User query: {user_input}")
                
                # Handle special commands
                if self._handle_special_command(user_input.strip().lower()):
                    continue
                
                # Translate natural language to SQL
                logger.debug("Translating natural language to SQL...")

                try:
                    query_schema_info, query_examples = self._query_context(user_input)

                    with self.executor.console.status(
                        "[cyan]Thinking... (waiting for LLM)[/cyan]", spinner="dots"
                    ):
                        result = self.llm.translate_to_sql(
                            user_input,
                            query_schema_info,
                            self.conversation_history,
                            examples=query_examples,
                        )

                    sql = result['sql']
                    explanation = result['explanation']
                    confidence = result.get('confidence', 'medium')

                    # Show confidence warning for low confidence queries
                    if confidence == 'low':
                        self.executor.display_warning(
                            "Low confidence in this query. Please verify the results."
                        )

                    # Deterministic guard: normalize dialect, enforce read-only,
                    # and repair hallucinated tables/columns before executing.
                    prepared = self._prepare_sql(
                        user_input, query_schema_info, sql, explanation
                    )
                    if prepared is None:
                        continue
                    sql, explanation = prepared

                    # Check for guessed literal values that don't exist in the data
                    sql = resolve_value_mismatches(
                        sql, self.db_inspector, self._ask_value_choice
                    )

                    # Cap unbounded result sets
                    dialect = getattr(self.db_inspector, "dialect", "sqlite")
                    sql = enforce_limit(sql, dialect=dialect)

                    # Execute and display
                    logger.debug(f"Executing SQL: {sql}")
                    success = self.executor.execute_and_display(
                        sql,
                        explanation,
                        show_sql=True
                    )
                    
                    # Update conversation history
                    if success:
                        logger.info("Query executed successfully")
                        self.conversation_history.append({
                            "role": "user",
                            "content": user_input
                        })
                        self.conversation_history.append({
                            "role": "assistant",
                            "content": f"SQL: {sql}\nExplanation: {explanation}"
                        })
                        
                        # Keep only last 10 exchanges to avoid context overflow
                        if len(self.conversation_history) > 20:
                            self.conversation_history = self.conversation_history[-20:]
                
                except ValueError as e:
                    logger.warning(f"Translation error: {e}")
                    self.executor.display_warning(f"Translation error: {e}")
                except RuntimeError as e:
                    logger.error(f"LLM error: {e}")
                    self.executor.display_warning(f"LLM error: {e}")
                
            except KeyboardInterrupt:
                raise
            except EOFError:
                break
            except typer.Exit:
                # Re-raise typer.Exit to allow clean exit
                raise
            except Exception as e:
                logger.exception("Unexpected error in interaction loop")
                self.executor.display_warning(f"Unexpected error: {e}")

    def _prepare_sql(
        self, user_input: str, schema_info: str, sql: str, explanation: str
    ) -> Optional[tuple]:
        """Normalize, validate, and (if needed) repair a generated query.

        Transpiles the query to target dialect, rejects anything that isn't read-only,
        and - when it references tables/columns that don't exist - asks the LLM
        for a single repair using the specific error as a hint.

        Returns:
            (sql, explanation) ready to run, or None if the query was rejected.
        """
        dialect = getattr(self.db_inspector, "dialect", "sqlite")
        sql = transpile_to_dialect(sql, dialect=dialect)

        if not is_read_only(sql, dialect=dialect):
            self.executor.display_warning(
                "Generated query contains potentially unsafe operations. "
                "Only SELECT queries are allowed."
            )
            return None

        problems = find_schema_problems(
            sql, build_schema(self.db_inspector), dialect=dialect
        )
        if problems:
            logger.info(f"Schema validation problems: {problems}")
            self.executor.display_info("Refining query to match the schema...")
            try:
                with self.executor.console.status(
                    "[cyan]Fixing query... (waiting for LLM)[/cyan]", spinner="dots"
                ):
                    repaired = self.llm.repair_sql(
                        user_input,
                        schema_info,
                        sql,
                        "; ".join(problems),
                        self.conversation_history,
                    )
                candidate = transpile_to_dialect(repaired["sql"], dialect=dialect)
                if is_read_only(candidate, dialect=dialect):
                    sql = candidate
                    explanation = repaired.get("explanation", explanation)
                    logger.info(f"Repaired SQL: {sql}")
            except Exception as e:
                logger.warning(f"SQL repair attempt failed: {e}")

        return sql, explanation

    def _ask_value_choice(
        self, column: str, attempted_value: str, candidates: list
    ) -> Optional[str]:
        """Ask the user to pick a real column value when the LLM guessed wrong.

        Args:
            column: Name of the column being filtered
            attempted_value: The literal value the LLM guessed
            candidates: Actual distinct values present in the column

        Returns:
            The value the user picked, or None to run the query unchanged
        """
        self.executor.display_warning(
            f"'{attempted_value}' was not found in column '{column}'."
        )
        self.executor.console.print(f"[cyan]Actual values for {column}:[/cyan]")
        for i, candidate in enumerate(candidates, 1):
            self.executor.console.print(f"  {i}. {candidate}")

        choice = Prompt.ask(
            "Pick a number to use instead, or press Enter to run the query as-is",
            default=""
        ).strip()

        if choice.isdigit():
            index = int(choice) - 1
            if 0 <= index < len(candidates):
                return candidates[index]

        return None

    def _handle_special_command(self, command: str) -> bool:
        """Handle special commands.
        
        Args:
            command: Command to handle
            
        Returns:
            True if command was handled, False otherwise
        """
        if command in ['exit', 'quit', 'q']:
            logger.info("User exiting application")
            typer.echo("\nGoodbye!")
            raise typer.Exit(0)
        
        elif command == 'schema':
            logger.debug("Displaying database schema")
            self.executor.display_schema()
            return True
        
        elif command == 'tables':
            logger.debug("Listing database tables")
            tables = self.db_inspector.get_tables()
            self.executor.display_info(f"Tables: {', '.join(tables)}")
            return True
        
        elif command == 'help':
            logger.debug("Displaying help")
            self._show_help()
            return True
        
        elif command == 'clear':
            logger.info("Clearing conversation history")
            self.conversation_history = []
            self.executor.display_info("Conversation history cleared")
            return True
        
        elif command == 'logs':
            logger.info("Showing logs directory")
            self.executor.display_info(f"Logs are stored in: {LOGS_DIR}")
            return True

        elif command == 'profile':
            if not self.db_inspector.profile:
                logger.info("Profiling database on demand")
                self._run_profiling()
            else:
                logger.info("Re-profiling database")
                self._run_profiling(force=True)
            # New value hints only reach the model through the schema text.
            self.schema_info = self.db_inspector.get_schema_description()
            self._show_profile()
            return True

        elif command == 'metadata':
            # Reload from disk: the authoring loop is write, ask, refine, so
            # needing a restart for every edit would be the main friction in it.
            logger.info("Reloading metadata")
            self._load_metadata(rebuild_prompt=True)
            self._show_metadata()
            return True

        return False

    def _show_metadata(self) -> None:
        """Display the curated metadata layer, and anything wrong with it."""
        metadata = self.db_inspector.metadata
        console = self.executor.console

        # Prose is canonical: note when the file carries one, so the reader knows
        # the structured blocks below are generated (and `--regenerate-metadata`
        # is the way to refresh them). Any staleness warning appears further down.
        sidecar = discover(self.source_path) if self.use_metadata else None
        if sidecar is not None and not load_prose(sidecar).is_empty:
            console.print(
                f"[dim]Prose present in {sidecar.name}; structured blocks below are "
                f"generated from it (regenerate with --regenerate-metadata).[/dim]"
            )

        if metadata is None or metadata.is_empty:
            self.executor.display_info(
                "No metadata loaded. Create a starter file with:\n"
                f"  sql-browser --init-metadata {self.source_path.name}"
            )
        else:
            database = metadata.database
            if database.description:
                console.print(f"\n[bold cyan]Database[/bold cyan]\n  {database.description}")
            if database.conventions:
                console.print("\n[bold cyan]Conventions[/bold cyan]")
                for key, value in database.conventions.items():
                    console.print(f"  [yellow]{key}[/yellow]: {value}")
            if database.rules:
                console.print("\n[bold cyan]Rules[/bold cyan]")
                for rule in database.rules:
                    console.print(f"  • {rule}")
            if database.glossary:
                console.print("\n[bold cyan]Glossary[/bold cyan]")
                for term in database.glossary:
                    console.print(f"  [yellow]{term.term}[/yellow]: {term.means}")

            for table, table_meta in metadata.tables.items():
                console.print(f"\n[bold cyan]{table}[/bold cyan]")
                if table_meta.description:
                    console.print(f"  {table_meta.description}")
                if table_meta.grain:
                    console.print(f"  [dim]one row per {table_meta.grain}[/dim]")
                for column, column_meta in table_meta.columns.items():
                    details = []
                    if column_meta.description:
                        details.append(column_meta.description)
                    if column_meta.values:
                        details.append(
                            ", ".join(
                                f"{code}={meaning.label}"
                                for code, meaning in column_meta.values.items()
                            )
                        )
                    if details:
                        console.print(f"  [yellow]{column}[/yellow]: {' | '.join(details)}")

            if metadata.relationships:
                console.print("\n[bold cyan]Relationships[/bold cyan]")
                for rel in metadata.relationships:
                    origin = "declared in the database" if rel.declared else "from metadata"
                    composite = " [yellow]composite[/yellow]" if rel.is_composite else ""
                    console.print(
                        f"  {rel.describe()}{composite} [dim]({origin})[/dim]"
                    )

        warnings = self.db_inspector.metadata_warnings
        if warnings:
            console.print(
                f"\n[bold yellow]{len(warnings)} warning(s)[/bold yellow] "
                "[dim](these entries were ignored)[/dim]"
            )
            for warning in warnings:
                console.print(f"  ⚠ {warning}")

    def _show_profile(self) -> None:
        """Display the cached column profile (values/ranges)."""
        if not any(self.db_inspector.profile.values()):
            self.executor.display_info("No columns were profiled (no low-cardinality or numeric columns found).")
            return

        for table, columns in self.db_inspector.profile.items():
            if not columns:
                continue
            self.executor.console.print(f"\n[bold cyan]{table}[/bold cyan]")
            for column, info in columns.items():
                if info["type"] == "enum":
                    values = ", ".join(str(v) for v in info["values"])
                    self.executor.console.print(f"  [yellow]{column}[/yellow]: {values}")
                elif info["type"] == "range":
                    self.executor.console.print(
                        f"  [yellow]{column}[/yellow]: {info['min']} to {info['max']}"
                    )

    def _show_help(self) -> None:
        """Display help information."""
        help_text = """
[bold cyan]SQL Browser Help[/bold cyan]

[yellow]Natural Language Queries:[/yellow]
  Just type your question in plain English!
  Examples:
    • "Show me all users"
    • "What are the top 10 products by price?"
    • "Count how many orders were placed last month"
    • "Find customers from California"

[yellow]Special Commands:[/yellow]
  • schema   - Display the complete database schema
  • tables   - List all tables in the database
  • profile  - Show (or refresh) profiled column values/ranges
  • metadata - Reload the metadata file and show it, with any warnings
  • clear    - Clear conversation history
  • logs     - Show logs directory location
  • help     - Show this help message
  • exit     - Exit the application (or use Ctrl+C)

[yellow]Tips:[/yellow]
  • Be specific in your questions for better results
  • The AI maintains conversation context
  • Only SELECT queries are allowed (read-only)
  • Low confidence queries will show a warning
"""
        self.executor.console.print(help_text)


def _init_metadata(
    source: Path, model: str, use_llm: bool, out: Optional[Path] = None
) -> None:
    """Write a starter metadata file for a database, then return.

    Kept separate from SQLBrowserSession because scaffolding needs no LLM at
    all unless --llm-draft is passed, and requiring Ollama to be running just
    to emit a YAML skeleton would be a pointless obstacle.

    Args:
        source: The database or workbook to document
        model: Ollama model to use for drafting
        use_llm: Whether to have the model draft descriptions
        out: Optional destination metadata file path

    Raises:
        typer.Exit: The target file already exists, or the workbook is unreadable
    """
    if out is not None:
        destination = out
    else:
        destination = source.with_suffix("")
        destination = destination.with_name(f"{destination.name}.meta.yaml")

    if destination.exists():
        # Never clobber documentation someone has written by hand.
        typer.echo(
            f"Error: {destination.name} already exists. Move or delete it first, "
            f"or edit it directly.",
            err=True,
        )
        raise typer.Exit(1)

    inspector = create_inspector(source)
    try:
        inspector.connect()

        # Profiling is what supplies the real categorical values the scaffold
        # pre-fills, so it's worth the wait here even though it's optional
        # elsewhere.
        typer.echo("Profiling database...")
        inspector.profile = profile_database(inspector)

        drafts = {}
        if use_llm:
            llm = LLMQueryTranslator(model=model)
            tables = inspector.get_tables()
            for index, table in enumerate(tables, 1):
                typer.echo(f"Drafting documentation for '{table}' ({index}/{len(tables)})...")
                drafts[table] = llm.draft_metadata(
                    table,
                    inspector.get_table_schema(table),
                    inspector.profile.get(table, {}),
                    inspector.get_table_sample(table, limit=3),
                )

        destination.write_text(
            build_scaffold(inspector, source.name, drafts=drafts), encoding="utf-8"
        )
    finally:
        inspector.disconnect()

    typer.echo(f"\nWrote {destination}")
    if use_llm:
        typer.echo("Review the drafted descriptions - the model guessed at them.")
    else:
        typer.echo("Fill in the descriptions that matter and delete the rest.")
    typer.echo("It is picked up automatically next time you open this file.")


def _count_items(metadata: Metadata) -> int:
    """Count the leaf facts kept after reconciliation, for the CLI report."""
    total = len(metadata.tables) + len(metadata.relationships) + len(metadata.examples)
    for table_meta in metadata.tables.values():
        total += len(table_meta.columns)
        for column_meta in table_meta.columns.values():
            total += len(column_meta.values)
    return total


def _regenerate_metadata(
    source: Path, model: str, metadata_path: Optional[Path] = None
) -> None:
    """Regenerate a sidecar's structured blocks from its canonical prose.

    The command-line twin of the web editor's Save: it parses the prose the
    file already carries, reconciles the result against the live schema, and
    rewrites the derived blocks - so a prose edit made in an editor becomes
    real metadata without opening the web UI. Unlike ``--init-metadata`` this
    *requires* an existing prose-bearing sidecar; it never creates one from
    scratch.

    Args:
        source: The database or workbook whose sidecar to regenerate
        model: Ollama model used to parse the prose
        metadata_path: Optional explicit metadata path

    Raises:
        typer.Exit: No sidecar exists, or its prose is empty (nothing to do)
    """
    sidecar = metadata_path or discover(source)
    if sidecar is None or not sidecar.exists():
        typer.echo(
            f"Error: no metadata file found for '{source.name}'. Create one first "
            f"with:\n  sql-browser --init-metadata {source.name}",
            err=True,
        )
        raise typer.Exit(1)

    prose = load_prose(sidecar)
    if prose.is_empty:
        typer.echo(
            f"Error: {sidecar.name} has no `prose:` block to regenerate from. "
            "Add prose (in the file or the web editor) first.",
            err=True,
        )
        raise typer.Exit(1)

    try:
        existing = load(sidecar)
    except MetadataError:
        # The prose is what we're regenerating from; unreadable structured
        # blocks just mean nothing to preserve for prose-less sections.
        existing = Metadata()

    inspector = create_inspector(source)
    try:
        inspector.connect()

        # Reconciliation needs the profiled categorical values to keep a code
        # dictionary the prose describes, exactly as the web Save path does.
        typer.echo("Profiling database...")
        inspector.profile = profile_database(inspector)

        llm = LLMQueryTranslator(model=model)
        typer.echo("Parsing prose into structured metadata...")
        raw, gen_warnings = regenerate_structured(
            prose, llm, inspector, existing=existing
        )
        reconciled, recon_warnings = reconcile(raw, inspector)

        write_document(
            sidecar, prose, raw, source_name=source.name, model=model
        )
    finally:
        inspector.disconnect()

    warnings = gen_warnings + recon_warnings
    typer.echo(
        f"\nWrote {sidecar} "
        f"({_count_items(reconciled)} fact(s) kept, {len(recon_warnings)} dropped)."
    )
    if warnings:
        typer.echo(f"\n{len(warnings)} warning(s):", err=True)
        for warning in warnings:
            typer.echo(f"  - {warning}", err=True)


class DefaultCommandGroup(typer.core.TyperGroup):
    """Click group that dispatches to default 'browse' command when no subcommand given."""

    def parse_args(self, ctx, args):
        if not args:
            return super().parse_args(ctx, ["browse"])
        first_token = None
        for a in args:
            if not a.startswith("-"):
                first_token = a
                break

        if first_token in self.commands or (first_token is None and any(a in ("--help", "-h") for a in args)):
            return super().parse_args(ctx, args)

        return super().parse_args(ctx, ["browse"] + list(args))


app = typer.Typer(
    cls=DefaultCommandGroup,
    name="sql-browser",
    help="SQL Browser - Natural language SQLite and DuckDB database explorer",
    add_completion=False,
    no_args_is_help=False,
)

metadata_app = typer.Typer(
    name="metadata",
    help="Metadata sidecar management (init, validate, regenerate)",
    add_completion=False,
)
app.add_typer(metadata_app, name="metadata")


def version_callback(value: bool):
    """Show version and exit."""
    if value:
        from . import __version__
        typer.echo(f"SQL Browser v{__version__}")
        raise typer.Exit()


@app.command(name="browse", hidden=True)
def cli(
    database: Optional[Path] = typer.Argument(
        None,
        help="Path to a SQLite or DuckDB database file, or an Excel workbook (.xlsx/.xlsm) "
        "whose sheets will be loaded as tables. Optional with --web: the file "
        "can be picked in the browser instead",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
    ),
    model: Optional[str] = typer.Option(
        None,
        "--model",
        "-m",
        help=f"Model to use, on the configured provider (default: {DEFAULT_MODEL})",
    ),
    no_profile: bool = typer.Option(
        False,
        "--no-profile",
        help="Skip profiling the database (column values/ranges) for text-to-SQL hints",
    ),
    metadata: Optional[Path] = typer.Option(
        None,
        "--metadata",
        help="Path to a metadata file, instead of looking for one next to the database",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
    ),
    no_metadata: bool = typer.Option(
        False,
        "--no-metadata",
        help="Ignore any sidecar metadata file (foreign keys are still read - "
        "those come from the database, not from metadata)",
    ),
    init_metadata: bool = typer.Option(
        False,
        "--init-metadata",
        help="Write a starter metadata file next to the database and exit, "
        "pre-filled with every table, column and categorical value",
    ),
    llm_draft: bool = typer.Option(
        False,
        "--llm-draft",
        help="With --init-metadata, have the LLM draft descriptions to correct "
        "(slower; one request per table)",
    ),
    regenerate_metadata: bool = typer.Option(
        False,
        "--regenerate-metadata",
        help="Regenerate an existing sidecar's structured blocks from its prose "
        "and exit (the command-line twin of the web editor's Save)",
    ),
    web: bool = typer.Option(
        False,
        "--web",
        help="Launch the browser-based UI instead of the terminal REPL",
    ),
    port: int = typer.Option(
        8000,
        "--port",
        help="Port for the web UI (only used with --web)",
    ),
    data_dir: Optional[Path] = typer.Option(
        None,
        "--data-dir",
        help="Directory the web UI stores uploaded files in "
        f"(default: {settings.data_dir})",
        file_okay=False,
        dir_okay=True,
    ),
    version: Optional[bool] = typer.Option(
        None,
        "--version",
        "-v",
        callback=version_callback,
        is_eager=True,
        help="Show version and exit",
    ),
) -> None:
    """SQL Browser - Natural language SQLite database explorer.

    Ask questions about your SQLite database in plain English and get SQL results.
    Excel workbooks (.xlsx/.xlsm) are also supported: each sheet is loaded as a
    table in a temporary SQLite database for the duration of the session.

    Examples:

      \b
      sql-browser mydata.db
      sql-browser mydata.duckdb
      sql-browser mydata.xlsx
      sql-browser --model gemma4:31b-cloud mydata.db
      sql-browser --no-profile mydata.db
      sql-browser --web mydata.db
      sql-browser --web                    # pick the file in the browser
      sql-browser --init-metadata mydata.db    # write mydata.meta.yaml
      sql-browser --regenerate-metadata mydata.db  # rebuild it from its prose
      sql-browser --metadata shared.meta.yaml mydata.db
    """
    if data_dir:
        settings.data_dir = data_dir

    # Contradictory, and silently ignoring the path you explicitly passed would
    # be the worst way to resolve it.
    if metadata and no_metadata:
        typer.echo(
            "Error: --metadata and --no-metadata contradict each other. "
            "Pass one or the other.",
            err=True,
        )
        raise typer.Exit(1)

    if llm_draft and not init_metadata:
        typer.echo(
            "Error: --llm-draft only applies with --init-metadata.",
            err=True,
        )
        raise typer.Exit(1)

    if init_metadata and regenerate_metadata:
        typer.echo(
            "Error: --init-metadata creates a new file; --regenerate-metadata "
            "rebuilds an existing one. Pass one or the other.",
            err=True,
        )
        raise typer.Exit(1)

    # Write a metadata skeleton and exit, rather than starting a session.
    if init_metadata:
        if database is None:
            typer.echo(
                "Error: --init-metadata needs a database or workbook to document.",
                err=True,
            )
            raise typer.Exit(1)
        try:
            _init_metadata(database, model or DEFAULT_MODEL, llm_draft)
        except ExcelLoadError as e:
            logger.error(f"Failed to load Excel workbook: {e}")
            typer.echo(f"Error: could not load '{database.name}':", err=True)
            for issue in e.issues:
                typer.echo(f"  - {issue}", err=True)
            raise typer.Exit(1)
        return

    # Regenerate an existing sidecar's structured blocks from its prose and exit.
    if regenerate_metadata:
        if database is None:
            typer.echo(
                "Error: --regenerate-metadata needs the database or workbook the "
                "sidecar documents.",
                err=True,
            )
            raise typer.Exit(1)
        try:
            _regenerate_metadata(database, model or DEFAULT_MODEL)
        except ExcelLoadError as e:
            logger.error(f"Failed to load Excel workbook: {e}")
            typer.echo(f"Error: could not load '{database.name}':", err=True)
            for issue in e.issues:
                typer.echo(f"  - {issue}", err=True)
            raise typer.Exit(1)
        return

    # Launch the web UI instead of the terminal REPL. The database is optional
    # here - it can be uploaded or picked from the data directory in the UI.
    if web:
        from .web import run_web

        run_web(
            str(database) if database else None,
            model=model,
            profile=not no_profile,
            port=port,
            metadata_path=metadata,
            use_metadata=not no_metadata,
        )
        return

    if database is None:
        typer.echo(
            "Error: a database or Excel workbook is required for the terminal REPL.\n"
            "Pass one as an argument, or use --web to pick a file in the browser.",
            err=True,
        )
        raise typer.Exit(1)

    # Start session
    try:
        session = SQLBrowserSession(
            str(database),
            model=model or DEFAULT_MODEL,
            profile=not no_profile,
            metadata_path=metadata,
            use_metadata=not no_metadata,
        )
    except ExcelLoadError as e:
        logger.error(f"Failed to load Excel workbook: {e}")
        typer.echo(f"Error: could not load '{database.name}':", err=True)
        for issue in e.issues:
            typer.echo(f"  - {issue}", err=True)
        raise typer.Exit(1)
    session.start()


@app.command(name="query")
def query_command(
    question: str = typer.Argument(
        ...,
        help="Natural language question to translate and execute",
    ),
    database: Path = typer.Argument(
        ...,
        help="Path to database file (.db, .sqlite, .duckdb, .parquet, .xlsx)",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
    ),
    metadata: Optional[Path] = typer.Option(
        None,
        "--metadata",
        help="Path to a metadata file, bypassing discovery",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
    ),
    no_metadata: bool = typer.Option(
        False,
        "--no-metadata",
        help="Ignore any sidecar metadata file",
    ),
    model: Optional[str] = typer.Option(
        None,
        "--model",
        "-m",
        help=f"Model to use, on the configured provider (default: {DEFAULT_MODEL})",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Output structured JSON to stdout",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Translate and repair query without executing",
    ),
) -> None:
    """Execute a natural language query against a database without interactive session."""
    if metadata and no_metadata:
        typer.echo(
            "Error: --metadata and --no-metadata contradict each other. Pass one or the other.",
            err=True,
        )
        raise typer.Exit(1)

    inspector = create_inspector(database)
    dialect = getattr(inspector, "dialect", "sqlite")

    try:
        inspector.connect()
    except Exception as e:
        logger.error(f"Failed to connect to database: {e}")
        if json_output:
            import json
            print(json.dumps({
                "question": question,
                "sql": "",
                "rows": [],
                "corrections": [],
                "notes": "",
                "warnings": [f"Could not connect: {e}"],
                "error": str(e)
            }, indent=2))
        else:
            typer.echo(f"Error: could not connect to {database}: {e}", err=True)
        raise typer.Exit(1)

    try:
        # Resolve metadata and profile
        meta, warnings = resolve_metadata(
            inspector,
            source_path=database,
            override=metadata,
            use_file=not no_metadata,
        )
        inspector.metadata = meta
        inspector.metadata_warnings = warnings
        inspector.profile = profile_database(inspector)

        # Context & prompt
        schema_info = inspector.get_schema_description(question=question)
        examples = select_examples(
            question, meta.examples if meta else []
        )

        llm = LLMQueryTranslator(model=model or DEFAULT_MODEL)
        all_warnings = list(warnings)
        corrections: List[str] = []

        try:
            res = llm.translate_to_sql(question, schema_info, [], examples=examples)
            sql = res.get("sql", "")
            notes = res.get("explanation", "")
            confidence = res.get("confidence", "medium")
            if confidence == "low":
                all_warnings.append("Low confidence in generated query")
        except Exception as e:
            logger.error(f"LLM translation error: {e}")
            if json_output:
                import json
                print(json.dumps({
                    "question": question,
                    "sql": "",
                    "rows": [],
                    "corrections": corrections,
                    "notes": "",
                    "warnings": all_warnings + [f"Translation error: {e}"],
                    "error": str(e)
                }, indent=2))
            else:
                typer.echo(f"Error: translation failed: {e}", err=True)
            raise typer.Exit(1)

        # SQL Guard & Repair
        sql = transpile_to_dialect(sql, dialect=dialect)
        if not is_read_only(sql, dialect=dialect):
            msg = "Generated query is not read-only. Only SELECT queries are allowed."
            logger.warning(msg)
            if json_output:
                import json
                print(json.dumps({
                    "question": question,
                    "sql": sql,
                    "rows": [],
                    "corrections": corrections,
                    "notes": notes,
                    "warnings": all_warnings,
                    "error": msg
                }, indent=2))
            else:
                typer.echo(f"Error: {msg}", err=True)
            raise typer.Exit(1)

        problems = find_schema_problems(
            sql, build_schema(inspector), dialect=dialect
        )
        if problems:
            corrections.append(f"Schema problems: {'; '.join(problems)}")
            try:
                repaired = llm.repair_sql(
                    question, schema_info, sql, "; ".join(problems), []
                )
                cand = transpile_to_dialect(repaired.get("sql", ""), dialect=dialect)
                if is_read_only(cand, dialect=dialect):
                    sql = cand
                    if repaired.get("explanation"):
                        notes = repaired["explanation"]
                    corrections.append("Query repaired to match schema")
            except Exception as e:
                all_warnings.append(f"Schema repair attempt failed: {e}")

        # Literal value correction
        fixed_sql, mismatches = find_value_mismatches(sql, inspector)
        if fixed_sql != sql:
            corrections.append("Auto-corrected literal values")
            sql = fixed_sql
        for m in mismatches:
            all_warnings.append(
                f"Literal value '{m.get('attempted_value')}' not found in column '{m.get('column')}'"
            )

        # Enforce limit
        sql = enforce_limit(sql, limit=1000, dialect=dialect)

        rows: List[Dict[str, Any]] = []
        if not dry_run:
            try:
                cols, raw_rows = inspector.execute_query(sql)
                rows = [dict(zip(cols, row)) for row in raw_rows]
            except Exception as e:
                corrections.append(f"Runtime error: {e}")
                repaired_ok = False
                try:
                    repaired = llm.repair_sql(question, schema_info, sql, str(e), [])
                    cand = transpile_to_dialect(repaired.get("sql", ""), dialect=dialect)
                    if is_read_only(cand, dialect=dialect):
                        cand = enforce_limit(cand, limit=1000, dialect=dialect)
                        cols, raw_rows = inspector.execute_query(cand)
                        sql = cand
                        rows = [dict(zip(cols, row)) for row in raw_rows]
                        corrections.append("Query repaired after runtime error")
                        repaired_ok = True
                except Exception:
                    pass

                if not repaired_ok:
                    if json_output:
                        import json
                        print(json.dumps({
                            "question": question,
                            "sql": sql,
                            "rows": [],
                            "corrections": corrections,
                            "notes": notes,
                            "warnings": all_warnings,
                            "error": str(e)
                        }, indent=2, default=str))
                    else:
                        typer.echo(f"Execution error: {e}", err=True)
                    raise typer.Exit(1)

        if json_output:
            import json
            payload = {
                "question": question,
                "sql": sql,
                "rows": rows,
                "corrections": corrections,
                "notes": notes,
                "warnings": all_warnings,
            }
            print(json.dumps(payload, indent=2, default=str))
        else:
            from rich.console import Console
            from rich.table import Table
            console = Console()
            console.print(f"[bold cyan]Question:[/bold cyan] {question}")
            console.print(f"[bold cyan]SQL:[/bold cyan] {sql}")
            if notes:
                console.print(f"[dim]{notes}[/dim]")
            if dry_run:
                console.print("[yellow]Dry run: query not executed.[/yellow]")
            else:
                if rows:
                    table_ui = Table(title="Results")
                    for col_name in rows[0].keys():
                        table_ui.add_column(str(col_name))
                    for r in rows:
                        table_ui.add_row(*[str(v) if v is not None else "" for v in r.values()])
                    console.print(table_ui)
                else:
                    console.print("[dim]No rows returned.[/dim]")
    finally:
        inspector.disconnect()


@metadata_app.command(name="init")
def metadata_init_command(
    database: Path = typer.Argument(
        ...,
        help="Path to database file (.db, .sqlite, .duckdb, .parquet, .xlsx)",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
    ),
    out: Optional[Path] = typer.Option(
        None,
        "--out",
        "-o",
        help="Output metadata file path (default: <database>.meta.yaml)",
    ),
    llm_draft: bool = typer.Option(
        False,
        "--llm-draft",
        help="Have LLM draft descriptions",
    ),
    model: Optional[str] = typer.Option(
        None,
        "--model",
        "-m",
        help=f"Model to use, on the configured provider (default: {DEFAULT_MODEL})",
    ),
) -> None:
    """Initialize metadata scaffold for a database file."""
    try:
        _init_metadata(database, model or DEFAULT_MODEL, llm_draft, out=out)
    except ExcelLoadError as e:
        logger.error(f"Failed to load Excel workbook: {e}")
        typer.echo(f"Error: could not load '{database.name}':", err=True)
        for issue in e.issues:
            typer.echo(f"  - {issue}", err=True)
        raise typer.Exit(1)


@metadata_app.command(name="validate")
def metadata_validate_command(
    database: Path = typer.Argument(
        ...,
        help="Path to database file (.db, .sqlite, .duckdb, .parquet, .xlsx)",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
    ),
    metadata: Optional[Path] = typer.Option(
        None,
        "--metadata",
        "-m",
        help="Path to metadata file (default: discover sidecar next to database)",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
    ),
) -> None:
    """Reconcile metadata against live schema and report warnings."""
    target_meta = metadata or discover(database)
    if target_meta is None or not target_meta.exists():
        typer.echo(
            f"Error: no metadata file found for '{database.name}'.", err=True
        )
        raise typer.Exit(1)

    try:
        inspector = create_inspector(database)
    except ExcelLoadError as e:
        logger.error(f"Failed to load Excel workbook: {e}")
        typer.echo(f"Error: could not load '{database.name}':", err=True)
        for issue in e.issues:
            typer.echo(f"  - {issue}", err=True)
        raise typer.Exit(1)

    try:
        inspector.connect()
        inspector.profile = profile_database(inspector)
        loaded = load(target_meta)
        reconciled_meta, warnings = reconcile(loaded, inspector)
    finally:
        inspector.disconnect()

    typer.echo(f"Validating {target_meta.name} against {database.name}...")
    typer.echo(f"Loaded {len(reconciled_meta.tables)} table(s), {len(reconciled_meta.relationships)} relationship(s).")
    if warnings:
        typer.echo(f"\n{len(warnings)} warning(s):", err=True)
        for w in warnings:
            typer.echo(f"  - {w}", err=True)
    else:
        typer.echo("Metadata is valid. 0 warnings.")


@metadata_app.command(name="regenerate")
def metadata_regenerate_command(
    database: Path = typer.Argument(
        ...,
        help="Path to database file (.db, .sqlite, .duckdb, .parquet, .xlsx)",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
    ),
    metadata: Optional[Path] = typer.Option(
        None,
        "--metadata",
        "-m",
        help="Path to metadata file (default: discover sidecar next to database)",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
    ),
    model: Optional[str] = typer.Option(
        None,
        "--model",
        help=f"Model to use, on the configured provider (default: {DEFAULT_MODEL})",
    ),
) -> None:
    """Regenerate structured metadata blocks from free-form prose."""
    try:
        _regenerate_metadata(database, model or DEFAULT_MODEL, metadata_path=metadata)
    except ExcelLoadError as e:
        logger.error(f"Failed to load Excel workbook: {e}")
        typer.echo(f"Error: could not load '{database.name}':", err=True)
        for issue in e.issues:
            typer.echo(f"  - {issue}", err=True)
        raise typer.Exit(1)


def main():
    """Entry point for the CLI."""
    app()


if __name__ == "__main__":
    app()

# Made with Bob
