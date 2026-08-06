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
"""

from typing import TYPE_CHECKING, Any, Dict, List, Optional

from .metadata import ColumnMeta, Metadata

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


def render_schema_context(db_inspector: "DatabaseInspector") -> str:
    """Build the full schema description handed to the LLM.

    Args:
        db_inspector: A connected inspector; its `profile` and `metadata` are
            used when populated and simply omitted when not

    Returns:
        The prompt-ready description of the database
    """
    schema = db_inspector.get_full_schema()
    metadata = db_inspector.metadata or Metadata()

    lines = ["Database Schema:\n"]
    lines += _render_preamble(metadata)

    for table_name, table_info in schema.items():
        table_meta = metadata.table(table_name)

        lines.append(f"\nTable: {table_name} ({table_info['row_count']} rows)")
        if table_meta.description:
            lines.append(f"  {_one_line(table_meta.description)}")
        if table_meta.grain:
            lines.append(f"  Grain: one row per {table_meta.grain}")
        if table_meta.synonyms:
            lines.append(f"  Also called: {', '.join(table_meta.synonyms)}")
        lines.append("Columns:")

        column_profiles = db_inspector.profile.get(table_name, {})
        for col in table_info["columns"]:
            lines.append(
                _render_column(
                    col,
                    column_profiles.get(col["name"]),
                    metadata.column(table_name, col["name"]),
                )
            )

    lines += _render_relationships(metadata)

    return "\n".join(lines)

# Made with Bob
