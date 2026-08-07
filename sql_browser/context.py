"""Renders everything the LLM knows about the database into prompt text.

This is the one place the layers are composed: the physical schema
(database.py), the statistical profile (profiler.py), the database's own
declared foreign keys, and the curated semantic layer (metadata.py) all meet
here and become a single string.

Keeping it in one module matters because prompt text is a shared resource.
Every fact competes for the model's attention and for a finite context window,
so decisions about what to include, how to phrase it, and what to truncate need
to be made together rather than scattered across whichever module happens to
own each fact.

With no metadata and no foreign keys, the output is the same schema listing the
tool has always produced - the enrichment is strictly additive, both in what it
claims and in what it prints.

**Relevance pruning.** Full per-column detail (types, notes, value dictionaries)
is only rendered for tables scored relevant to the current question; every other
table still gets one line, so it's never invisible, just terse. This is what
keeps a 40-table database's prompt from drowning both the model's attention and
its context window in tables the question doesn't need - see BACKLOG.md #1 for
the measurements that motivated it. Scoring is lexical (question words against
table/column names and metadata synonyms/value labels), then expanded one hop
along the join graph so a needed bridge table is never dropped, and falls back
to "include everything" whenever it can't confidently narrow anything down -
pruning must never be the reason a valid query fails. Passing no question (the
default) renders every table in full, which is the original, unpruned
behaviour - existing callers that don't yet have a question to prune against
are unaffected.
"""

import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set

from .metadata import ColumnMeta, ExampleQuery, Metadata, Relationship

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance
    from .database import DatabaseInspector

#: Cap on enum values inlined per column so a database with many categorical
#: columns doesn't blow up the prompt. The full list stays in the profile for
#: query correction.
MAX_ENUM_VALUES_IN_SCHEMA = 8

#: A higher cap applies once values carry curated labels: someone chose to write
#: that dictionary down, and truncating it discards exactly the meaning it was
#: written to convey.
MAX_LABELLED_VALUES_IN_SCHEMA = 20

#: How many curated examples to hand the model as prior turns. Kept small - a
#: handful of well-matched demonstrations beats a wall of loosely related ones,
#: and every example costs as much prompt space as a real conversation turn.
DEFAULT_MAX_EXAMPLES = 3


def _one_line(text: Optional[str]) -> str:
    """Collapse authored prose onto a single line.

    YAML's folded (`>`) and literal (`|`) scalars are the natural way to write a
    sentence of documentation, and both keep a trailing newline. Since almost
    everything here is rendered as one item in a list, that newline would show
    up as a blank line between entries.
    """
    return " ".join((text or "").split())


def _clean_note(text: Optional[str]) -> str:
    """Normalize one clause of a column's annotation.

    Notes are joined with "; ", so a trailing full stop from a prose description
    would render as "line total.; money".
    """
    return _one_line(text).rstrip(".;,")


# --------------------------------------------------------------------------- #
# Lexical relevance scoring
# --------------------------------------------------------------------------- #

_TOKEN_RE = re.compile(r"[a-z0-9]+")

#: Words too common to carry any relevance signal on their own. Deliberately
#: short and conservative - a stopword that's actually a table or column name
#: (e.g. a table literally called "order") would silently make that table
#: unreachable by name, so this only removes words no schema is likely to use.
_STOPWORDS = {
    "a", "an", "the", "of", "in", "on", "for", "and", "or", "is", "are",
    "was", "were", "to", "from", "by", "with", "at", "as", "that", "this",
    "these", "those", "how", "what", "which", "who", "whom", "where", "when",
    "why", "many", "much", "me", "my", "i", "we", "us", "show", "list", "get",
    "find", "give", "all", "each", "per", "please", "do", "does", "did",
    "have", "has", "had", "can", "could", "would", "should", "there", "it",
    "its", "me", "please",
}


def _token_variants(token: str) -> Set[str]:
    """A token plus a naive singular form, so 'customer' matches 'customers'.

    Not real stemming - just also offering the word with a trailing 's'
    stripped - and it's additive (the original is always kept) so it can only
    ever create an extra match, never break an exact one like 'status'.
    """
    variants = {token}
    if len(token) > 3 and token.endswith("s"):
        variants.add(token[:-1])
    return variants


def _tokenize(text: str) -> Set[str]:
    """Split text into lowercase word tokens, including singular variants."""
    variants: Set[str] = set()
    for raw in _TOKEN_RE.findall(text.lower()):
        variants |= _token_variants(raw)
    return variants


def _question_terms(question: str) -> Set[str]:
    """Tokenize a question, dropping stopwords before variant expansion."""
    variants: Set[str] = set()
    for raw in _TOKEN_RE.findall(question.lower()):
        if raw in _STOPWORDS:
            continue
        variants |= _token_variants(raw)
    return variants


def _table_terms(table_name: str, table_meta) -> Set[str]:
    """Words that identify a table: its name and any authored synonyms."""
    terms = _tokenize(table_name)
    for synonym in table_meta.synonyms:
        terms |= _tokenize(synonym)
    return terms


def _column_terms(column_name: str, column_meta: ColumnMeta) -> Set[str]:
    """Words that identify a column: its name, synonyms, and value labels.

    Value labels/synonyms matter because a question naming a value ("orders
    that are shipped") should pull in the table that value lives on, even
    though "shipped" itself never appears as a table or column name.
    """
    terms = _tokenize(column_name)
    for synonym in column_meta.synonyms:
        terms |= _tokenize(synonym)
    for meaning in column_meta.values.values():
        terms |= _tokenize(meaning.label)
        for synonym in meaning.synonyms:
            terms |= _tokenize(synonym)
    return terms


def _expand_one_hop(tables: Set[str], relationships: List[Relationship]) -> Set[str]:
    """Add every table one join away from `tables`, so a bridge is never lost.

    A question naming two tables that only join through a third (e.g.
    customers and products, joined via orders and order_items) must still
    render that third table in full, or the model has no way to write the
    join at all.
    """
    expanded = set(tables)
    for rel in relationships:
        if rel.from_table in tables:
            expanded.add(rel.to_table)
        if rel.to_table in tables:
            expanded.add(rel.from_table)
    return expanded


def relevant_tables(
    question: str, schema: Dict[str, Any], metadata: Metadata
) -> Optional[Set[str]]:
    """Score tables against a question for prompt pruning.

    Lexical only, by design (see BACKLOG.md #1 for the embeddings-later
    rationale): question words are matched against table/column names and the
    synonyms and value labels a metadata file already provides, then expanded
    one hop along the join graph.

    Args:
        question: The user's natural-language question
        schema: `DatabaseInspector.get_full_schema()` output
        metadata: Reconciled metadata (possibly empty)

    Returns:
        The relevant table names, or None if the question yielded no usable
        tokens or matched nothing - callers should treat None as "scoring
        couldn't narrow this down, include everything" rather than as "nothing
        is relevant". A false inclusion costs prompt space; a false exclusion
        costs correctness, so ties go to inclusion.
    """
    question_terms = _question_terms(question)
    if not question_terms:
        return None

    matched: Set[str] = set()
    for table_name, table_info in schema.items():
        table_meta = metadata.table(table_name)
        terms = _table_terms(table_name, table_meta)
        for col in table_info["columns"]:
            terms |= _column_terms(col["name"], metadata.column(table_name, col["name"]))
        if question_terms & terms:
            matched.add(table_name)

    if not matched:
        return None

    return _expand_one_hop(matched, metadata.relationships)


def select_examples(
    question: str,
    examples: List[ExampleQuery],
    limit: int = DEFAULT_MAX_EXAMPLES,
) -> List[Dict[str, str]]:
    """Pick the curated examples most relevant to a question.

    Same lexical approach as `relevant_tables`, scored against each example's
    own question text. Unlike table pruning, an example that matches nothing
    is simply dropped rather than included by default: a table can be needed
    without being named, but an unrelated example has no such excuse and a
    small model is prone to copying its shape regardless of fit - so the safe
    default here is fewer examples, not more.

    Args:
        question: The user's natural-language question
        examples: Reconciled examples (already schema-validated)
        limit: Maximum number of examples to return, most relevant first

    Returns:
        Up to `limit` `{"question": ..., "sql": ...}` dicts, ready to hand to
        `LLMQueryTranslator.translate_to_sql` - plain dicts rather than the
        pydantic model, so llm.py doesn't need to import metadata.py's types.
    """
    if not examples:
        return []

    question_terms = _question_terms(question)
    if not question_terms:
        return []

    scored = []
    for example in examples:
        overlap = len(question_terms & _tokenize(example.question))
        if overlap:
            scored.append((overlap, example))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [
        {"question": example.question, "sql": example.sql}
        for _, example in scored[:limit]
    ]


# --------------------------------------------------------------------------- #
# Column-level rendering
# --------------------------------------------------------------------------- #

def format_profile_hint(column_profile: Optional[Dict[str, Any]]) -> str:
    """Format a profiled column's known values/range as a schema hint."""
    if not column_profile:
        return ""

    if column_profile["type"] == "enum":
        values = column_profile["values"]
        shown = values[:MAX_ENUM_VALUES_IN_SCHEMA]
        formatted = ", ".join(repr(v) for v in shown)
        if len(values) > len(shown):
            formatted += f", ... ({len(values) - len(shown)} more)"
        return f" (values: {formatted})"

    if column_profile["type"] == "range":
        return f" (range: {column_profile['min']} to {column_profile['max']})"

    return ""


def _format_labelled_values(
    column_profile: Optional[Dict[str, Any]], column_meta: ColumnMeta
) -> str:
    """Format an enum's values with their curated meanings attached.

    The codes come from the data (the profile), never from the metadata file, so
    a documented-but-absent code can't appear here. Codes without a label are
    still listed - a partially documented column is the normal case while
    someone is still filling the file in.
    """
    if column_profile and column_profile.get("type") == "enum":
        codes = [str(v) for v in column_profile["values"]]
    else:
        # No profile to go on (column wasn't enum-profiled): fall back to the
        # documented codes, which reconciliation has already verified exist.
        codes = list(column_meta.values.keys())

    if not codes:
        return ""

    shown = codes[:MAX_LABELLED_VALUES_IN_SCHEMA]
    parts = []
    for code in shown:
        meaning = column_meta.values.get(code)
        if meaning is None:
            parts.append(repr(code))
            continue
        rendered = f"{code!r}={meaning.label}"
        if meaning.synonyms:
            rendered += f" [aka {', '.join(meaning.synonyms)}]"
        parts.append(rendered)

    formatted = ", ".join(parts)
    if len(codes) > len(shown):
        formatted += f", ... ({len(codes) - len(shown)} more)"
    return f" (values: {formatted})"


def _render_column(
    col: Dict[str, Any],
    column_profile: Optional[Dict[str, Any]],
    column_meta: ColumnMeta,
) -> str:
    """Render one column line: physical facts first, then meaning."""
    pk_marker = " [PRIMARY KEY]" if col["pk"] else ""
    null_marker = " NOT NULL" if col["notnull"] else ""
    line = f"  - {col['name']}: {col['type']}{pk_marker}{null_marker}"

    notes: List[str] = []
    if column_meta.description:
        notes.append(_clean_note(column_meta.description))
    if column_meta.synonyms:
        notes.append(f"also called {', '.join(column_meta.synonyms)}")
    if column_meta.semantic_type:
        unit = f" in {column_meta.unit}" if column_meta.unit else ""
        notes.append(f"{column_meta.semantic_type}{unit}")
    elif column_meta.unit:
        notes.append(f"unit: {column_meta.unit}")
    if column_meta.default_time_column:
        notes.append("use this column for relative date filters")
    if column_meta.pii:
        notes.append("PII - avoid selecting unless asked for")

    if notes:
        line += " - " + "; ".join(notes)

    # Curated labels supersede the bare value list; they contain it.
    if column_meta.values:
        line += _format_labelled_values(column_profile, column_meta)
    else:
        line += format_profile_hint(column_profile)

    return line


# --------------------------------------------------------------------------- #
# Document-level rendering
# --------------------------------------------------------------------------- #

def _render_preamble(metadata: Metadata) -> List[str]:
    """Render the database-wide blocks that precede the table listing."""
    lines: List[str] = []
    db = metadata.database

    if db.description:
        lines += ["", "DATABASE", _one_line(db.description)]

    if db.conventions:
        lines += ["", "CONVENTIONS"]
        lines += [
            f"  - {key}: {_one_line(value)}" for key, value in db.conventions.items()
        ]

    if db.rules:
        # Phrased as an instruction because a bare list gets treated as trivia.
        lines += ["", "RULES (apply to every query unless the user overrides them)"]
        lines += [f"  {i}. {_one_line(rule)}" for i, rule in enumerate(db.rules, 1)]

    if db.glossary:
        lines += ["", "GLOSSARY (what the user means by these terms)"]
        lines += [
            f"  - {term.term}: {_one_line(term.means)}" for term in db.glossary
        ]

    return lines


def _render_relationships(metadata: Metadata) -> List[str]:
    """Render the join graph, the one thing a model cannot guess reliably."""
    if not metadata.relationships:
        return []

    lines = ["", "RELATIONSHIPS (use only these join paths)"]
    for rel in metadata.relationships:
        detail = []
        if rel.cardinality:
            detail.append(rel.cardinality)
        if rel.is_composite:
            # Spelled out because joining a composite key on one column fans out
            # and silently returns wrong rows - the model has to use all of them.
            detail.append("composite key: join on ALL of these columns together")
        if rel.note:
            detail.append(_one_line(rel.note))
        suffix = f" ({'; '.join(detail)})" if detail else ""
        lines.append(f"  - {rel.describe()}{suffix}")
    return lines


def _render_table_full(
    table_name: str,
    table_info: Dict[str, Any],
    table_meta,
    metadata: Metadata,
    column_profiles: Dict[str, Any],
) -> List[str]:
    """Render a table with full per-column detail: types, notes, values."""
    lines = [f"\nTable: {table_name} ({table_info['row_count']} rows)"]
    if table_meta.description:
        lines.append(f"  {_one_line(table_meta.description)}")
    if table_meta.grain:
        lines.append(f"  Grain: one row per {table_meta.grain}")
    if table_meta.synonyms:
        lines.append(f"  Also called: {', '.join(table_meta.synonyms)}")
    lines.append("Columns:")

    for col in table_info["columns"]:
        lines.append(
            _render_column(
                col,
                column_profiles.get(col["name"]),
                metadata.column(table_name, col["name"]),
            )
        )
    return lines


def _render_table_brief(
    table_name: str, table_info: Dict[str, Any], table_meta
) -> List[str]:
    """Render a table pruned as not relevant: name and columns, nothing else.

    Deliberately not empty - a pruned table still needs to be reachable by
    name and its real column names still need to be visible, or a question
    that scoring got wrong (or a join the model discovers it needs mid-query)
    would have no way to reference it correctly. What's dropped is exactly the
    per-column detail (types, notes, value dictionaries) pruning exists to
    save: the bulk of the prompt cost, and the part a table only needs when
    it's actually in play.
    """
    lines = [f"\nTable: {table_name} ({table_info['row_count']} rows)"]
    if table_meta.description:
        lines.append(f"  {_one_line(table_meta.description)}")
    if table_meta.synonyms:
        lines.append(f"  Also called: {', '.join(table_meta.synonyms)}")
    column_names = ", ".join(col["name"] for col in table_info["columns"])
    lines.append(f"  Columns: {column_names}")
    return lines


def render_schema_context(
    db_inspector: "DatabaseInspector", question: Optional[str] = None
) -> str:
    """Build the schema description handed to the LLM.

    Args:
        db_inspector: A connected inspector; its `profile` and `metadata` are
            used when populated and simply omitted when not
        question: The user's natural-language question, used to prune full
            column detail down to tables scored relevant (see
            `relevant_tables`). None - the default - renders every table in
            full, exactly as before this parameter existed; pass it whenever a
            question is actually available so a wide database's prompt scales.

    Returns:
        The prompt-ready description of the database
    """
    schema = db_inspector.get_full_schema()
    metadata = db_inspector.metadata or Metadata()

    lines = ["Database Schema:\n"]
    lines += _render_preamble(metadata)

    relevant = relevant_tables(question, schema, metadata) if question else None

    for table_name, table_info in schema.items():
        table_meta = metadata.table(table_name)
        if relevant is None or table_name in relevant:
            lines += _render_table_full(
                table_name,
                table_info,
                table_meta,
                metadata,
                db_inspector.profile.get(table_name, {}),
            )
        else:
            lines += _render_table_brief(table_name, table_info, table_meta)

    lines += _render_relationships(metadata)

    return "\n".join(lines)

# Made with Bob
