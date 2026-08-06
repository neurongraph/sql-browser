"""Command-line interface for SQL Browser."""

import sys
from pathlib import Path
from typing import Optional

import typer
from rich.prompt import Prompt

from .database import DatabaseInspector
from .excel_loader import EXCEL_EXTENSIONS, ExcelLoadError, convert_xlsx_to_sqlite
from .llm import LLMQueryTranslator
from .profiler import profile_database
from .query_corrector import resolve_value_mismatches
from .query_executor import QueryExecutor
from .sql_guard import (
    build_schema,
    enforce_limit,
    find_schema_problems,
    is_read_only,
    transpile_to_sqlite,
)
from .logger import get_logger, LOGS_DIR

logger = get_logger(__name__)


class SQLBrowserSession:
    """Manages an interactive SQL Browser session."""

    def __init__(self, db_path: str, model: str = "gemma4:31b-cloud", profile: bool = True):
        """Initialize the session.

        Args:
            db_path: Path to the SQLite database, or an Excel workbook
                (each sheet becomes a table in a temporary SQLite database)
            model: Ollama model to use
            profile: Whether to profile the database (column value/range
                hints) at startup to improve text-to-SQL accuracy
        """
        self.source_path = Path(db_path)
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

            # Get schema for LLM context
            schema_info = self.db_inspector.get_schema_description()

            # Show suggestions
            with self.executor.console.status(
                "[cyan]Waiting for LLM to generate suggestions...[/cyan]", spinner="dots"
            ):
                suggestions = self.llm.get_query_suggestions(schema_info)
            self.executor.display_suggestions(suggestions)
            
            # Main interaction loop
            self._interaction_loop(schema_info)
            
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

    def _interaction_loop(self, schema_info: str) -> None:
        """Main interaction loop.
        
        Args:
            schema_info: Database schema description
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
                    with self.executor.console.status(
                        "[cyan]Thinking... (waiting for LLM)[/cyan]", spinner="dots"
                    ):
                        result = self.llm.translate_to_sql(
                            user_input,
                            schema_info,
                            self.conversation_history
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
                    prepared = self._prepare_sql(user_input, schema_info, sql, explanation)
                    if prepared is None:
                        continue
                    sql, explanation = prepared

                    # Check for guessed literal values that don't exist in the data
                    sql = resolve_value_mismatches(
                        sql, self.db_inspector, self._ask_value_choice
                    )

                    # Cap unbounded result sets
                    sql = enforce_limit(sql)

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

        Transpiles the query to SQLite, rejects anything that isn't read-only,
        and - when it references tables/columns that don't exist - asks the LLM
        for a single repair using the specific error as a hint.

        Returns:
            (sql, explanation) ready to run, or None if the query was rejected.
        """
        sql = transpile_to_sqlite(sql)

        if not is_read_only(sql):
            self.executor.display_warning(
                "Generated query contains potentially unsafe operations. "
                "Only SELECT queries are allowed."
            )
            return None

        problems = find_schema_problems(sql, build_schema(self.db_inspector))
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
                candidate = transpile_to_sqlite(repaired["sql"])
                if is_read_only(candidate):
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
            self._show_profile()
            return True

        return False

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
  • schema  - Display the complete database schema
  • tables  - List all tables in the database
  • profile - Show (or refresh) profiled column values/ranges
  • clear   - Clear conversation history
  • logs    - Show logs directory location
  • help    - Show this help message
  • exit    - Exit the application (or use Ctrl+C)

[yellow]Tips:[/yellow]
  • Be specific in your questions for better results
  • The AI maintains conversation context
  • Only SELECT queries are allowed (read-only)
  • Low confidence queries will show a warning
"""
        self.executor.console.print(help_text)


app = typer.Typer(
    name="sql-browser",
    help="SQL Browser - Natural language SQLite database explorer",
    add_completion=False
)


def version_callback(value: bool):
    """Show version and exit."""
    if value:
        from . import __version__
        typer.echo(f"SQL Browser v{__version__}")
        raise typer.Exit()


@app.command(name="sql-browser")
def cli(
    database: Path = typer.Argument(
        ...,
        help="Path to a SQLite database file, or an Excel workbook (.xlsx/.xlsm) "
        "whose sheets will be loaded as tables",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
    ),
    model: str = typer.Option(
        "gemma4:31b-cloud",
        "--model",
        "-m",
        help="Ollama model to use",
    ),
    no_profile: bool = typer.Option(
        False,
        "--no-profile",
        help="Skip profiling the database (column values/ranges) for text-to-SQL hints",
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
      sql-browser mydata.xlsx
      sql-browser --model gemma4:31b-cloud mydata.db
      sql-browser --no-profile mydata.db
      sql-browser --web mydata.db
    """
    # Launch the web UI instead of the terminal REPL
    if web:
        from .web import run_web

        run_web(
            str(database),
            model=model,
            profile=not no_profile,
            port=port,
        )
        return

    # Start session
    try:
        session = SQLBrowserSession(str(database), model=model, profile=not no_profile)
    except ExcelLoadError as e:
        logger.error(f"Failed to load Excel workbook: {e}")
        typer.echo(f"Error: could not load '{database.name}':", err=True)
        for issue in e.issues:
            typer.echo(f"  - {issue}", err=True)
        raise typer.Exit(1)
    session.start()


def main():
    """Entry point for the CLI."""
    app()


if __name__ == "__main__":
    app()

# Made with Bob
