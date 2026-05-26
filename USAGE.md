# SQL Browser Usage Guide

## Quick Start

1. **Install Ollama** (if not already installed):
   ```bash
   # Visit https://ollama.ai/ and follow installation instructions
   # Then pull a model:
   ollama pull llama3.2
   ```

2. **Install dependencies**:
   ```bash
   uv sync
   ```

3. **Create a sample database** (optional):
   ```bash
   uv run python examples/create_sample_db.py
   ```

4. **Run SQL Browser**:
   ```bash
   uv run sql-browser sample.db
   ```

## Example Queries

Once the application is running, you can ask questions in natural language:

### Basic Queries
- "Show me all tables"
- "What are the columns in the customers table?"
- "Show me 5 sample customers"

### Data Exploration
- "How many customers do we have?"
- "List all products in the Electronics category"
- "Show me the top 10 most expensive products"
- "Find all orders from the last 30 days"

### Aggregations
- "What is the total revenue from all orders?"
- "Show me the average order value"
- "Count how many orders each customer has made"
- "What are the top 5 best-selling products?"

### Filtering
- "Find customers from California"
- "Show me all pending orders"
- "List products with stock less than 50"
- "Find orders with total amount greater than $500"

### Joins
- "Show me customer names with their order totals"
- "List all order items with product names"
- "Find which customers bought laptops"

## Special Commands

While in the interactive session:

- `schema` - Display the complete database schema
- `tables` - List all tables in the database
- `clear` - Clear conversation history
- `help` - Show help message
- `exit` or `quit` - Exit the application

## Tips

1. **Be Specific**: The more specific your question, the better the results
   - ❌ "Show me data"
   - ✅ "Show me the top 10 customers by total order value"

2. **Context Matters**: The AI maintains conversation context
   - First: "Show me all customers from California"
   - Then: "How many orders have they made?" (refers to CA customers)

3. **Check Confidence**: Low confidence queries will show a warning
   - Review the generated SQL to ensure it matches your intent

4. **Read-Only**: Only SELECT queries are allowed for safety
   - No INSERT, UPDATE, DELETE, or DROP operations

5. **Schema Awareness**: Use `schema` command to see available tables and columns
   - Helps you ask more accurate questions

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

## Troubleshooting

### "Failed to connect to Ollama"
- Make sure Ollama is running: `ollama serve`
- Check if the model is available: `ollama list`
- Pull the model if needed: `ollama pull llama3.2`

### "Database file not found"
- Verify the path to your database file
- Use absolute path if relative path doesn't work
- Create sample database: `uv run python examples/create_sample_db.py`

### Low Quality Results
- Try being more specific in your questions
- Use the `schema` command to understand the database structure
- Try a different model with `--model` option
- Clear conversation history with `clear` command

### Import Errors
- Run `uv sync` to install all dependencies
- Make sure you're using Python 3.10 or higher

## Advanced Usage

### Using with Your Own Database

```bash
uv run sql-browser /path/to/your/database.db
```

### Programmatic Usage

You can also use SQL Browser as a library:

```python
from sql_browser.database import DatabaseInspector
from sql_browser.llm import LLMQueryTranslator
from sql_browser.query_executor import QueryExecutor

# Initialize components
db = DatabaseInspector("mydata.db")
llm = LLMQueryTranslator(model="llama3.2")

with db:
    executor = QueryExecutor(db)
    schema = db.get_schema_description()
    
    # Translate natural language to SQL
    result = llm.translate_to_sql(
        "Show me all customers",
        schema
    )
    
    # Execute and display
    executor.execute_and_display(
        result['sql'],
        result['explanation']
    )
```

## Examples with Sample Database

After creating the sample database, try these queries:

```
🔍 Show me all customers from California

🔍 What are the top 5 products by price?

🔍 How many orders were placed in the last 30 days?

🔍 Show me the total revenue by product category

🔍 Find customers who have spent more than $1000

🔍 List all delivered orders with customer names

🔍 What is the average order value?

🔍 Show me products that are low in stock (less than 30)

🔍 Which customers have never placed an order?

🔍 What are the most popular products by quantity sold?