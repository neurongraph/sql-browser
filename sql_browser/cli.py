"""Command-line interface for SQL Browser."""

import sys
from pathlib import Path
from typing import Optional

import typer
from rich.prompt import Prompt

from .database import DatabaseInspector
from .llm import LLMQueryTranslator
from .query_executor import QueryExecutor
from .logger import get_logger, LOGS_DIR

logger = get_logger(__name__)


class SQLBrowserSession:
    """Manages an interactive SQL Browser session."""

    def __init__(self, db_path: str, model: str = "llama3.2"):
        """Initialize the session.
        
        Args:
            db_path: Path to the SQLite database
            model: Ollama model to use
        """
        self.db_path = db_path
        self.db_inspector = DatabaseInspector(db_path)
        self.llm = LLMQueryTranslator(model=model)
        self.executor: Optional[QueryExecutor] = None
        self.conversation_history = []

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
            self.executor.display_welcome(self.db_path)
            
            # Get schema for LLM context
            schema_info = self.db_inspector.get_schema_description()
            
            # Show suggestions
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
                self.executor.display_info("Thinking...")
                logger.debug("Translating natural language to SQL...")
                
                try:
                    result = self.llm.translate_to_sql(
                        user_input,
                        schema_info,
                        self.conversation_history
                    )
                    
                    sql = result['sql']
                    explanation = result['explanation']
                    confidence = result.get('confidence', 'medium')
                    
                    # Validate SQL
                    if not self.llm.validate_sql(sql):
                        self.executor.display_warning(
                            "Generated query contains potentially unsafe operations. "
                            "Only SELECT queries are allowed."
                        )
                        continue
                    
                    # Show confidence warning for low confidence queries
                    if confidence == 'low':
                        self.executor.display_warning(
                            "Low confidence in this query. Please verify the results."
                        )
                    
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
        
        return False

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
        help="Path to the SQLite database file",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
    ),
    model: str = typer.Option(
        "llama3.2",
        "--model",
        "-m",
        help="Ollama model to use",
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
    
    Examples:
    
      \b
      sql-browser mydata.db
      sql-browser --model llama3.1 mydata.db
    """
    # Start session
    session = SQLBrowserSession(str(database), model=model)
    session.start()


def main():
    """Entry point for the CLI."""
    app()


if __name__ == "__main__":
    app()

# Made with Bob
