"""LLM integration for natural language to SQL translation."""

import json
import time
from typing import Dict, Optional

import ollama
from json_repair import repair_json
from .config import DEFAULT_MODEL
from .logger import get_logger

logger = get_logger(__name__)


def list_models() -> list[str]:
    """Return the names of the models Ollama has available, sorted.

    Used to populate the web UI's model selector. Returns an empty list if
    Ollama isn't reachable so the UI can render (and say so) instead of
    failing - the model in use is reported separately.
    """
    try:
        response = ollama.list()
    except Exception as e:
        logger.warning(f"Could not list Ollama models: {e}")
        return []

    # ollama-python returns a ListResponse of Model objects on recent versions
    # and a plain dict on older ones; support both.
    entries = getattr(response, "models", None)
    if entries is None:
        entries = response.get("models", []) if isinstance(response, dict) else []

    names = []
    for entry in entries:
        name = getattr(entry, "model", None)
        if name is None and isinstance(entry, dict):
            name = entry.get("model") or entry.get("name")
        if name:
            names.append(str(name))

    return sorted(names, key=str.lower)


class LLMQueryTranslator:
    """Translates natural language queries to SQL using Ollama."""

    def __init__(self, model: str = DEFAULT_MODEL):
        """Initialize the LLM translator.
        
        Args:
            model: Name of the Ollama model to use
        """
        self.model = model
        self._verify_ollama()

    def _verify_ollama(self) -> None:
        """Verify that Ollama is running and the model is available."""
        try:
            # Check if Ollama is running
            ollama.list()
        except Exception as e:
            raise RuntimeError(
                f"Failed to connect to Ollama. Make sure Ollama is running.\n"
                f"Install from: https://ollama.ai/\n"
                f"Error: {e}"
            )

    def translate_to_sql(
        self, 
        natural_query: str, 
        schema_info: str,
        conversation_history: Optional[list] = None
    ) -> Dict[str, str]:
        """Translate a natural language query to SQL.
        
        Args:
            natural_query: The user's question in natural language
            schema_info: Database schema information
            conversation_history: Previous conversation for context
            
        Returns:
            Dictionary with 'sql' and 'explanation' keys
        """
        system_prompt = self._build_system_prompt(schema_info)
        
        messages = [
            {"role": "system", "content": system_prompt}
        ]
        
        # Add conversation history if provided
        if conversation_history:
            messages.extend(conversation_history)
        
        # Add current query
        messages.append({
            "role": "user",
            "content": natural_query
        })

        logger.debug(f"Natural query: {natural_query}")
        return self._request_sql(messages)

    def repair_sql(
        self,
        natural_query: str,
        schema_info: str,
        failed_sql: str,
        error: str,
        conversation_history: Optional[list] = None,
    ) -> Dict[str, str]:
        """Ask the LLM to fix a query that failed validation or execution.

        Feeds the failed SQL and the specific error back so the model can
        correct a hallucinated column/table or a bad expression, rather than
        regenerating blind.

        Args:
            natural_query: The user's original question
            schema_info: Database schema information
            failed_sql: The query that failed
            error: The error/validation message describing what went wrong
            conversation_history: Previous conversation for context

        Returns:
            Dictionary with 'sql', 'explanation' and 'confidence' keys
        """
        messages = [{"role": "system", "content": self._build_system_prompt(schema_info)}]
        if conversation_history:
            messages.extend(conversation_history)
        messages.append({"role": "user", "content": natural_query})
        messages.append({"role": "assistant", "content": json.dumps({"sql": failed_sql})})
        messages.append({
            "role": "user",
            "content": (
                f"That query is invalid:\n{error}\n\n"
                "Fix it. Use ONLY the tables and columns listed in the schema above. "
                "Respond in the same JSON format."
            ),
        })

        logger.info(f"Requesting SQL repair. Error: {error}")
        return self._request_sql(messages)

    def _request_sql(self, messages: list) -> Dict[str, str]:
        """Send a chat request expecting a JSON SQL response and parse it.

        Shared by translate_to_sql and repair_sql: performs the Ollama call,
        parses (and repairs) the JSON response, and validates its structure.
        """
        try:
            prompt_chars = sum(len(m["content"]) for m in messages)
            logger.info(
                f"Sending request to Ollama model: {self.model} "
                f"(prompt ~{prompt_chars} chars, ~{prompt_chars // 4} tokens)"
            )
            logger.debug(f"Messages: {json.dumps(messages, indent=2)}")

            start = time.monotonic()
            response = ollama.chat(
                model=self.model,
                messages=messages,
                format="json"
            )
            logger.info(f"Ollama responded in {time.monotonic() - start:.1f}s")

            # Log the raw response (convert to dict first)
            try:
                response_dict = dict(response) if hasattr(response, '__dict__') else response
                logger.debug(f"Raw response from Ollama: {response_dict}")
            except Exception as e:
                logger.debug(f"Could not serialize response for logging: {e}")
                logger.debug(f"Response type: {type(response)}")

            content = response['message']['content'].strip()
            logger.debug(f"Response content: {content}")

            # Handle empty response
            if not content:
                logger.error("LLM returned empty response")
                raise ValueError("LLM returned empty response")

            # Strip markdown code blocks if present
            if content.startswith('```'):
                logger.debug("Removing markdown code block wrapper")
                # Remove ```json or ``` at start and ``` at end
                lines = content.split('\n')
                if lines[0].startswith('```'):
                    lines = lines[1:]  # Remove first line
                if lines and lines[-1].strip() == '```':
                    lines = lines[:-1]  # Remove last line
                content = '\n'.join(lines).strip()
                logger.debug(f"Content after removing markdown: {content}")

            # Try to parse JSON with standard parser first
            try:
                result = json.loads(content)
                logger.debug(f"Parsed JSON result with standard parser: {json.dumps(result, indent=2)}")
            except json.JSONDecodeError as e:
                logger.warning(f"Standard JSON parsing failed: {e}")
                logger.debug(f"Content that failed standard parsing: {content}")

                # Try to repair and parse the JSON
                try:
                    logger.info("Attempting to repair malformed JSON...")
                    repaired_content = repair_json(content)
                    logger.debug(f"Repaired JSON: {repaired_content}")
                    result = json.loads(repaired_content)
                    logger.info("Successfully repaired and parsed JSON")
                    logger.debug(f"Parsed result: {json.dumps(result, indent=2)}")
                except Exception as repair_error:
                    logger.error(f"JSON repair also failed: {repair_error}")
                    logger.error(f"Original content: {content}")
                    # If both parsing attempts fail, show the actual response for debugging
                    raise ValueError(
                        f"Failed to parse LLM response as JSON even after repair attempt.\n"
                        f"Original error: {e}\n"
                        f"Repair error: {repair_error}\n"
                        f"Response was: {content[:200]}..."
                    )

            # Validate response structure
            if 'sql' not in result:
                logger.error(f"LLM response missing 'sql' field. Keys: {list(result.keys())}")
                raise ValueError(
                    f"LLM response missing 'sql' field. "
                    f"Response keys: {list(result.keys())}"
                )

            sql_result = {
                'sql': result.get('sql', ''),
                'explanation': result.get('explanation', 'No explanation provided'),
                'confidence': result.get('confidence', 'medium')
            }

            logger.info(f"Successfully translated query to SQL: {sql_result['sql']}")
            logger.debug(f"Explanation: {sql_result['explanation']}")
            logger.debug(f"Confidence: {sql_result['confidence']}")

            return sql_result

        except json.JSONDecodeError as e:
            logger.exception("JSON decode error")
            raise ValueError(f"Failed to parse LLM response as JSON: {e}")
        except KeyError as e:
            logger.exception("Key error in response")
            raise RuntimeError(f"Unexpected response structure from LLM: {e}")
        except Exception as e:
            logger.exception("Unexpected error in LLM communication")
            raise RuntimeError(f"Error communicating with LLM: {e}")

    def _build_system_prompt(self, schema_info: str) -> str:
        """Build the system prompt with schema information.
        
        Args:
            schema_info: Database schema description
            
        Returns:
            Formatted system prompt
        """
        return f"""You are an expert SQL query generator. Your task is to convert natural language questions into valid SQLite queries.

{schema_info}

IMPORTANT RULES:
1. Generate ONLY valid SQLite syntax
2. Use proper table and column names from the schema above
3. Always respond in JSON format with these fields:
   - "sql": the SQL query (required)
   - "explanation": brief explanation of what the query does (required)
   - "confidence": your confidence level - "high", "medium", or "low" (required)

4. For ambiguous queries, make reasonable assumptions and note them in the explanation
5. Use appropriate SQL clauses: SELECT, WHERE, JOIN, GROUP BY, ORDER BY, LIMIT as needed
6. For "show me" or "list" queries, use SELECT with appropriate LIMIT (default 10)
7. For "count" or "how many" queries, use COUNT()
8. For "top" or "highest" queries, use ORDER BY with LIMIT
9. Always use proper SQL formatting and indentation

EXAMPLE RESPONSES:

User: "Show me all tables"
{{
  "sql": "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name",
  "explanation": "Lists all user tables in the database, excluding system tables",
  "confidence": "high"
}}

User: "Find users created in the last month"
{{
  "sql": "SELECT * FROM users WHERE created_at >= date('now', '-1 month') ORDER BY created_at DESC",
  "explanation": "Retrieves all users created within the last 30 days, sorted by creation date",
  "confidence": "high"
}}

User: "What's the average price?"
{{
  "sql": "SELECT AVG(price) as average_price FROM products",
  "explanation": "Calculates the average price across all products. Assumes 'products' table has a 'price' column.",
  "confidence": "medium"
}}

Now, convert the user's natural language query to SQL following these rules."""

    def validate_sql(self, sql: str) -> bool:
        """Return True if the query is a single read-only SELECT.

        Delegates to the sqlglot-based guard, which parses the query rather
        than scanning for keywords - so it won't false-reject a column named
        ``created_at`` and it rejects stacked statements like
        ``SELECT ...; DROP TABLE x``.

        Args:
            sql: SQL query to validate

        Returns:
            True if query is read-only, False otherwise
        """
        from .sql_guard import is_read_only

        return is_read_only(sql)

    def get_query_suggestions(self, schema_info: str) -> list[str]:
        """Generate example queries based on the schema.
        
        Args:
            schema_info: Database schema description
            
        Returns:
            List of suggested natural language queries
        """
        prompt = f"""Based on this database schema, suggest 5 interesting natural language questions a user might ask:

{schema_info}

Respond in JSON format with a "suggestions" array containing 5 strings.
Example: {{"suggestions": ["Show me all users", "What is the total revenue?", ...]}}"""

        try:
            logger.info(
                f"Sending suggestions request to Ollama model: {self.model} "
                f"(prompt ~{len(prompt)} chars, ~{len(prompt) // 4} tokens)"
            )
            start = time.monotonic()
            response = ollama.chat(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                format="json"
            )
            logger.info(f"Ollama responded in {time.monotonic() - start:.1f}s")

            result = json.loads(response['message']['content'])
            return result.get('suggestions', [])

        except Exception:
            # Return generic suggestions if LLM fails
            return [
                "Show me all tables",
                "What are the columns in each table?",
                "Show me some sample data",
                "How many rows are in each table?",
                "What is the structure of the database?"
            ]

# Made with Bob
