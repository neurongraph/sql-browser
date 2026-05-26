"""Query execution and result formatting."""

from typing import Any, Dict, List, Tuple

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

from .database import DatabaseInspector


class QueryExecutor:
    """Executes SQL queries and formats results for display."""

    def __init__(self, db_inspector: DatabaseInspector):
        """Initialize query executor.
        
        Args:
            db_inspector: Database inspector instance
        """
        self.db_inspector = db_inspector
        self.console = Console()

    def execute_and_display(
        self, 
        sql: str, 
        explanation: str = "",
        show_sql: bool = True
    ) -> bool:
        """Execute a SQL query and display results.
        
        Args:
            sql: SQL query to execute
            explanation: Optional explanation of the query
            show_sql: Whether to display the SQL query
            
        Returns:
            True if execution was successful, False otherwise
        """
        try:
            # Display the SQL query if requested
            if show_sql:
                self._display_sql(sql, explanation)
            
            # Execute the query
            column_names, rows = self.db_inspector.execute_query(sql)
            
            # Display results
            if rows:
                self._display_results(column_names, rows)
                self.console.print(f"\n[dim]Returned {len(rows)} row(s)[/dim]")
            else:
                self.console.print("[yellow]No results found[/yellow]")
            
            return True
            
        except Exception as e:
            self._display_error(str(e))
            return False

    def _display_sql(self, sql: str, explanation: str = "") -> None:
        """Display the SQL query with syntax highlighting.
        
        Args:
            sql: SQL query to display
            explanation: Optional explanation
        """
        # Create syntax-highlighted SQL
        syntax = Syntax(sql, "sql", theme="monokai", line_numbers=False)
        
        # Create panel with SQL
        if explanation:
            title = f"[bold cyan]SQL Query[/bold cyan] - {explanation}"
        else:
            title = "[bold cyan]SQL Query[/bold cyan]"
        
        panel = Panel(syntax, title=title, border_style="cyan")
        self.console.print(panel)
        self.console.print()

    def _display_results(self, column_names: List[str], rows: List[Tuple]) -> None:
        """Display query results in a formatted table.
        
        Args:
            column_names: List of column names
            rows: List of result rows
        """
        # Create a rich table
        table = Table(show_header=True, header_style="bold magenta", show_lines=True)
        
        # Add columns
        for col_name in column_names:
            table.add_column(col_name, style="cyan")
        
        # Add rows
        for row in rows:
            # Convert row values to strings, handling None values
            str_row = [str(val) if val is not None else "[dim]NULL[/dim]" for val in row]
            table.add_row(*str_row)
        
        self.console.print(table)

    def _display_error(self, error_message: str) -> None:
        """Display an error message.
        
        Args:
            error_message: Error message to display
        """
        panel = Panel(
            f"[red]{error_message}[/red]",
            title="[bold red]Error[/bold red]",
            border_style="red"
        )
        self.console.print(panel)

    def display_schema(self) -> None:
        """Display the database schema in a formatted way."""
        schema = self.db_inspector.get_full_schema()
        
        self.console.print("\n[bold cyan]Database Schema[/bold cyan]\n")
        
        for table_name, table_info in schema.items():
            # Create table for each database table
            table = Table(
                title=f"[bold]{table_name}[/bold] ({table_info['row_count']} rows)",
                show_header=True,
                header_style="bold yellow",
                border_style="blue"
            )
            
            table.add_column("Column", style="cyan")
            table.add_column("Type", style="green")
            table.add_column("Constraints", style="magenta")
            
            for col in table_info['columns']:
                constraints = []
                if col['pk']:
                    constraints.append("PRIMARY KEY")
                if col['notnull']:
                    constraints.append("NOT NULL")
                if col['default_value']:
                    constraints.append(f"DEFAULT {col['default_value']}")
                
                constraint_str = ", ".join(constraints) if constraints else "-"
                
                table.add_row(
                    col['name'],
                    col['type'],
                    constraint_str
                )
            
            self.console.print(table)
            self.console.print()

    def display_welcome(self, db_path: str) -> None:
        """Display welcome message.
        
        Args:
            db_path: Path to the database file
        """
        welcome_text = Text()
        welcome_text.append("SQL Browser\n", style="bold cyan")
        welcome_text.append(f"Connected to: {db_path}\n", style="dim")
        welcome_text.append("\nType your questions in natural language, or:\n", style="white")
        welcome_text.append("  • ", style="dim")
        welcome_text.append("'schema'", style="yellow")
        welcome_text.append(" - Show database schema\n", style="white")
        welcome_text.append("  • ", style="dim")
        welcome_text.append("'tables'", style="yellow")
        welcome_text.append(" - List all tables\n", style="white")
        welcome_text.append("  • ", style="dim")
        welcome_text.append("'help'", style="yellow")
        welcome_text.append(" - Show help message\n", style="white")
        welcome_text.append("  • ", style="dim")
        welcome_text.append("'exit'", style="yellow")
        welcome_text.append(" or ", style="white")
        welcome_text.append("'quit'", style="yellow")
        welcome_text.append(" - Exit the application\n", style="white")
        
        panel = Panel(welcome_text, border_style="cyan", padding=(1, 2))
        self.console.print(panel)

    def display_suggestions(self, suggestions: List[str]) -> None:
        """Display query suggestions.
        
        Args:
            suggestions: List of suggested queries
        """
        self.console.print("\n[bold cyan]Try asking:[/bold cyan]")
        for i, suggestion in enumerate(suggestions, 1):
            self.console.print(f"  {i}. [yellow]{suggestion}[/yellow]")
        self.console.print()

    def display_info(self, message: str) -> None:
        """Display an informational message.
        
        Args:
            message: Message to display
        """
        self.console.print(f"[cyan]{message}[/cyan]")

    def display_warning(self, message: str) -> None:
        """Display a warning message.
        
        Args:
            message: Warning message to display
        """
        self.console.print(f"[yellow]⚠ {message}[/yellow]")

# Made with Bob
