"""Curated metadata: the semantic layer the database itself can't provide.

Schema inspection (database.py) and profiling (profiler.py) tell the LLM what a
database *is* - tables, types, the values a column actually holds. Neither can
tell it what any of that *means*: that `status = 'S'` is what a user calls
"shipped", that `orders` joins `customers` through `customer_id`, that money is
stored in cents, or that test rows should always be excluded. Those are the
mistakes a local model actually makes, and no amount of introspection fixes
them - the knowledge lives with whoever owns the data.

So this module loads a hand-authored (or LLM-drafted) YAML sidecar found next
to the database, and reconciles it against the real schema. Two rules govern
the design:

**Metadata is additive, never authoritative.** Types, keys and the set of values
a column contains always come from the database. A `values:` block only attaches
labels and synonyms to codes that already exist. Anything in the file that
doesn't exist in the database is dropped and reported as a warning. The result
is a guarantee worth having: a stale or wrong metadata file can make the prompt
less helpful, but it can never make the tool assert something false about the
data.

**Metadata never breaks a session.** A missing file, unparseable YAML or a
schema that has drifted underneath all degrade to warnings, exactly as a failed
profiling run does. You always get a usable browser.

Declared foreign keys (``PRAGMA foreign_key_list``) are folded into the same
relationship list, so a SQLite file with real constraints gets a join graph with
no authoring at all - and an Excel workbook, which has no constraints to read,
gets one as soon as someone writes the relationships down.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .database import DatabaseInspector
from .logger import get_logger
from .sql_guard import build_schema, find_schema_problems

logger = get_logger(__name__)

#: Schema version understood by this module. A file declaring anything else is
#: still loaded (fields are optional, so a newer file mostly degrades cleanly)
#: but the mismatch is reported.
SUPPORTED_VERSION = 1

#: Sidecar filenames tried in order, relative to the source file's directory.
#: `<stem>.meta.yaml` wins so a directory of databases can each have their own.
SIDECAR_SUFFIXES = (".meta.yaml", ".meta.yml")
SIDECAR_SHARED_NAMES = ("sql-browser.meta.yaml", "sql-browser.meta.yml")

#: How many distinct values to pull when verifying a `values:` block against a
#: column that wasn't profiled as an enum. If a column has more distinct values
#: than this we can't enumerate them, so declared codes are kept unverified
#: rather than wrongly dropped.
VALUE_CHECK_LIMIT = 200


class MetadataError(Exception):
    """Raised when a metadata file exists but cannot be read or parsed."""


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #

class _LenientModel(BaseModel):
    """Base model that treats a key with nothing under it as absent.

    In YAML, a key whose body is only comments parses as None:

        rules: []
        relationships:
          # - from: orders.customer_id

    That is what a half-filled-in file looks like, and it should load, not
    raise. Dropping None-valued keys lets each field's default apply.
    """

    @model_validator(mode="before")
    @classmethod
    def _drop_nulls(cls, data: Any) -> Any:
        if isinstance(data, dict):
            return {key: value for key, value in data.items() if value is not None}
        return data


class ColumnValue(_LenientModel):
    """What one code in a column means, e.g. 'S' -> "Shipped"."""

    label: str
    synonyms: List[str] = Field(default_factory=list)

    @classmethod
    def coerce(cls, value: Any) -> Any:
        """Allow the shorthand ``P: Pending`` alongside ``P: {label: Pending}``."""
        if isinstance(value, str):
            return {"label": value}
        return value


class ColumnMeta(_LenientModel):
    """Curated facts about one column. Every field is optional."""

    description: Optional[str] = None
    synonyms: List[str] = Field(default_factory=list)
    #: Free-form semantic tag (money, timestamp, percentage, code, ...). Carried
    #: into the prompt as a hint; deliberately not an enum so a team can invent
    #: the vocabulary that suits their data.
    semantic_type: Optional[str] = None
    unit: Optional[str] = None
    values: Dict[str, ColumnValue] = Field(default_factory=dict)
    pii: bool = False
    #: Marks the column to use for relative time filters ("last month") when the
    #: user names no column. At most one per table is meaningful.
    default_time_column: bool = False

    @field_validator("values", mode="before")
    @classmethod
    def _normalize_values(cls, value: Any) -> Any:
        """Stringify YAML keys, expand the shorthand, and drop unfilled entries.

        YAML turns an unquoted `1:` into an int key and `true:` into a bool, so
        keys are coerced to str here - they're compared against SQL literals as
        text downstream.

        Entries with a blank label are dropped, because a generated scaffold
        pre-fills the real codes and leaves the meanings empty: until someone
        types them, `'CA': ""` has nothing to contribute and must not reach the
        prompt as `'CA'=`.
        """
        if not isinstance(value, dict):
            return value

        normalized = {}
        for code, meaning in value.items():
            coerced = ColumnValue.coerce(meaning)
            if isinstance(coerced, dict) and not str(coerced.get("label") or "").strip():
                continue
            normalized[str(code)] = coerced
        return normalized


class TableMeta(_LenientModel):
    """Curated facts about one table."""

    description: Optional[str] = None
    synonyms: List[str] = Field(default_factory=list)
    #: What one row represents ("order", "order line", "customer-month"). The
    #: single most useful thing to state: it's how a model knows whether to
    #: count rows or count distinct keys.
    grain: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    columns: Dict[str, ColumnMeta] = Field(default_factory=dict)


class Relationship(_LenientModel):
    """A join edge between two tables, written as `table.column` on each side.

    A composite key is expressed by giving both sides a comma-separated column
    list - `from: offices.country, offices.region_code`. Keeping the columns of
    one constraint together matters: joining a composite key on only one of its
    columns fans out and returns wrong rows, so presenting them as separate
    edges would be presenting an invalid join path as valid.
    """

    model_config = ConfigDict(populate_by_name=True)

    #: `from` is a Python keyword, so the field is `from_` with a YAML alias.
    from_: str = Field(alias="from")
    to: str
    cardinality: Optional[str] = None
    note: Optional[str] = None
    #: True when this edge came from PRAGMA foreign_key_list rather than the file.
    declared: bool = False

    @field_validator("from_", "to", mode="before")
    @classmethod
    def _normalize_refs(cls, value: Any) -> Any:
        """Accept a YAML list of columns as well as a comma-separated string."""
        if isinstance(value, (list, tuple)):
            return ", ".join(str(item) for item in value)
        return value

    @property
    def from_refs(self) -> List[str]:
        """The `table.column` references on the child side, in key order."""
        return _split_refs(self.from_)

    @property
    def to_refs(self) -> List[str]:
        """The `table.column` references on the parent side, in key order."""
        return _split_refs(self.to)

    @property
    def from_table(self) -> str:
        return _split_ref(self.from_refs[0])[0] if self.from_refs else ""

    @property
    def to_table(self) -> str:
        return _split_ref(self.to_refs[0])[0] if self.to_refs else ""

    @property
    def is_composite(self) -> bool:
        return len(self.from_refs) > 1

    @property
    def key(self) -> Tuple[str, str]:
        """Identity for de-duplicating file-declared against PRAGMA-derived edges."""
        return (self.from_, self.to)

    def describe(self) -> str:
        """Render the edge for a prompt or a UI.

        A composite key is grouped so it reads as one join on several columns
        rather than as several separate joins:
        ``offices.(country, region_code) -> regions.(country, code)``
        """
        return f"{_group_refs(self.from_refs)} -> {_group_refs(self.to_refs)}"


class GlossaryTerm(_LenientModel):
    """A business term and what it means in this database."""

    term: str
    means: str


class ExampleQuery(_LenientModel):
    """A curated question -> SQL pair, used to few-shot the model.

    Rendered as an actual prior turn in the chat (see
    `LLMQueryTranslator.translate_to_sql`), not as prose in the schema text -
    a demonstrated format is followed far more reliably than a described one,
    which is the whole point of curating these. Reconciliation runs the SQL
    through `sql_guard.find_schema_problems()` and drops anything that
    doesn't bind against the live schema: an example referencing a dropped
    column would actively teach the model the wrong thing, which is worse
    than no example at all.
    """

    question: str
    sql: str


class DatabaseMeta(_LenientModel):
    """Database-wide context: what this data is and how to read it."""

    description: Optional[str] = None
    #: Cross-cutting storage conventions, e.g. {"currency": "cents"}.
    conventions: Dict[str, str] = Field(default_factory=dict)
    glossary: List[GlossaryTerm] = Field(default_factory=list)
    #: Standing instructions applied to every query ("exclude is_test = 1").
    rules: List[str] = Field(default_factory=list)


class Metadata(_LenientModel):
    """A whole metadata document."""

    version: int = SUPPORTED_VERSION
    database: DatabaseMeta = Field(default_factory=DatabaseMeta)
    tables: Dict[str, TableMeta] = Field(default_factory=dict)
    relationships: List[Relationship] = Field(default_factory=list)
    examples: List[ExampleQuery] = Field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        """True when there's nothing worth adding to a prompt."""
        db = self.database
        return not (
            db.description
            or db.conventions
            or db.glossary
            or db.rules
            or self.tables
            or self.relationships
            or self.examples
        )

    def table(self, name: str) -> TableMeta:
        """Return a table's metadata, or an empty one - callers needn't branch."""
        return self.tables.get(name) or TableMeta()

    def column(self, table: str, column: str) -> ColumnMeta:
        """Return a column's metadata, or an empty one."""
        return self.table(table).columns.get(column) or ColumnMeta()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _split_ref(ref: str) -> Tuple[str, str]:
    """Split a `table.column` reference. Returns ("", ref) if malformed."""
    table, _, column = ref.partition(".")
    if not column:
        return "", ref
    return table, column


def _split_refs(refs: str) -> List[str]:
    """Split one side of a relationship into its `table.column` references."""
    return [part.strip() for part in refs.split(",") if part.strip()]


def _group_refs(refs: List[str]) -> str:
    """Render one side of an edge, collapsing a composite key onto its table.

    ``["offices.country", "offices.region_code"]`` becomes
    ``offices.(country, region_code)`` so the columns read as one key. Falls
    back to a plain join when the references don't share a table, which only
    happens in a malformed file.
    """
    if not refs:
        return ""
    if len(refs) == 1:
        return refs[0]

    tables = {_split_ref(ref)[0] for ref in refs}
    if len(tables) != 1:
        return ", ".join(refs)
    table = next(iter(tables))
    columns = ", ".join(_split_ref(ref)[1] for ref in refs)
    return f"{table}.({columns})"


# --------------------------------------------------------------------------- #
# Discovery and loading
# --------------------------------------------------------------------------- #

def discover(source_path: Path) -> Optional[Path]:
    """Find the sidecar metadata file for a source file, if there is one.

    Note this takes the *source* path, not the connected database: an Excel
    workbook is converted to a throwaway SQLite file, and the metadata lives
    next to the workbook the user actually maintains.

    Args:
        source_path: The database or workbook the user opened

    Returns:
        Path to the metadata file, or None if none of the candidates exist
    """
    directory = source_path.parent
    candidates = [
        directory / f"{source_path.stem}{suffix}" for suffix in SIDECAR_SUFFIXES
    ]
    candidates += [directory / name for name in SIDECAR_SHARED_NAMES]

    for candidate in candidates:
        if candidate.is_file():
            logger.info(f"Found metadata file: {candidate}")
            return candidate
    return None


def load(path: Path) -> Metadata:
    """Load and validate a metadata file.

    Args:
        path: Path to a YAML metadata file

    Returns:
        The parsed metadata

    Raises:
        MetadataError: The file can't be read, isn't a YAML mapping, or doesn't
            match the expected structure
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise MetadataError(f"could not read {path.name}: {e}")

    # An empty file is a legitimate starting point, not an error.
    if raw is None:
        return Metadata()
    if not isinstance(raw, dict):
        raise MetadataError(
            f"{path.name}: expected a YAML mapping at the top level, got {type(raw).__name__}"
        )

    try:
        return Metadata.model_validate(raw)
    except Exception as e:
        raise MetadataError(f"{path.name}: {e}")


# --------------------------------------------------------------------------- #
# Reconciliation against the real schema
# --------------------------------------------------------------------------- #

def _foreign_key_relationships(db_inspector: DatabaseInspector) -> List[Relationship]:
    """Build relationships from the database's own declared foreign keys.

    One relationship per constraint, so the columns of a composite key stay
    together as a single join path.
    """
    relationships = []
    for table in db_inspector.get_tables():
        for fk in db_inspector.get_foreign_keys(table):
            child_refs = [f"{table}.{child}" for child, _ in fk["columns"]]
            parent_refs = [f"{fk['table']}.{parent}" for _, parent in fk["columns"]]
            if not child_refs:
                continue
            relationships.append(
                Relationship(
                    from_=", ".join(child_refs),
                    to=", ".join(parent_refs),
                    cardinality="many_to_one",
                    declared=True,
                )
            )
    return relationships


def _actual_values(
    db_inspector: DatabaseInspector, table: str, column: str
) -> Optional[set]:
    """Return the set of values a column holds, as strings, or None if unknown.

    Prefers the cached enum profile - a column worth documenting with a value
    dictionary is low-cardinality, so the profiler has almost always already
    enumerated it. Falls back to a bounded DISTINCT query, and returns None if
    that hits the bound, meaning "can't enumerate, don't drop anything".
    """
    profile_entry = db_inspector.profile.get(table, {}).get(column)
    if profile_entry and profile_entry.get("type") == "enum":
        return {str(v) for v in profile_entry["values"]}

    try:
        values = db_inspector.get_distinct_values(table, column, limit=VALUE_CHECK_LIMIT)
    except Exception as e:
        logger.debug(f"Could not read distinct values for {table}.{column}: {e}")
        return None

    if len(values) >= VALUE_CHECK_LIMIT:
        return None
    return {str(v) for v in values if v is not None}


def _reconcile_columns(
    db_inspector: DatabaseInspector,
    table: str,
    table_meta: TableMeta,
    real_columns: set,
    warnings: List[str],
) -> Dict[str, ColumnMeta]:
    """Drop column entries, and value codes, that don't exist in the database."""
    kept: Dict[str, ColumnMeta] = {}

    for column, column_meta in table_meta.columns.items():
        if column not in real_columns:
            warnings.append(f"{table}.{column}: no such column - ignored")
            continue

        if column_meta.values:
            actual = _actual_values(db_inspector, table, column)
            if actual is not None:
                missing = [code for code in column_meta.values if code not in actual]
                if missing:
                    warnings.append(
                        f"{table}.{column}: value(s) not present in the data - "
                        f"ignored: {', '.join(repr(c) for c in missing)}"
                    )
                    column_meta = column_meta.model_copy(
                        update={
                            "values": {
                                code: meaning
                                for code, meaning in column_meta.values.items()
                                if code in actual
                            }
                        }
                    )

        _warn_on_ambiguous_labels(table, column, column_meta, warnings)
        kept[column] = column_meta

    return kept


def _warn_on_ambiguous_labels(
    table: str, column: str, column_meta: ColumnMeta, warnings: List[str]
) -> None:
    """Report labels or synonyms that don't identify a single code.

    Worth surfacing rather than logging quietly: the whole point of a value
    dictionary is that a phrase maps to one code, so a collision means the
    dictionary can't do its job for that phrase - and resolve_label will decline
    to rewrite it rather than guess.
    """
    values = column_meta.values
    if len(values) < 2:
        return

    claims: Dict[str, List[str]] = {}
    for code, meaning in values.items():
        for phrase in [meaning.label, *meaning.synonyms]:
            normalized = phrase.strip().lower()
            if normalized:
                claims.setdefault(normalized, []).append(code)

    for phrase, codes in claims.items():
        if len(codes) > 1:
            warnings.append(
                f"{table}.{column}: {phrase!r} is claimed by "
                f"{', '.join(repr(c) for c in codes)} - too ambiguous to resolve, "
                f"so it won't be corrected automatically"
            )

    # A label that is also a different code is confusing for the same reason,
    # though harmless in practice: a literal matching a real code is left alone
    # before labels are ever consulted.
    for code, meaning in values.items():
        normalized = meaning.label.strip().lower()
        collides = [
            other for other in values if other != code and other.lower() == normalized
        ]
        if collides:
            warnings.append(
                f"{table}.{column}: label {meaning.label!r} on {code!r} is also the "
                f"code {collides[0]!r} - the code wins"
            )


def _reconcile_relationships(
    metadata: Metadata,
    db_inspector: DatabaseInspector,
    columns_by_table: Dict[str, set],
    warnings: List[str],
) -> List[Relationship]:
    """Validate authored edges, then union in the declared foreign keys."""
    kept: List[Relationship] = []
    seen: set = set()

    def endpoint_exists(ref: str) -> bool:
        table, column = _split_ref(ref)
        return bool(table) and column in columns_by_table.get(table, set())

    for relationship in metadata.relationships:
        from_refs, to_refs = relationship.from_refs, relationship.to_refs

        if not from_refs or not to_refs:
            warnings.append(
                f"relationship {relationship.describe()}: "
                f"both sides need a table.column reference - ignored"
            )
            continue
        # A composite key must pair up, or the join it describes is unusable.
        if len(from_refs) != len(to_refs):
            warnings.append(
                f"relationship {relationship.describe()}: "
                f"{len(from_refs)} column(s) on the left but {len(to_refs)} on the "
                f"right - ignored"
            )
            continue
        if not all(endpoint_exists(ref) for ref in from_refs + to_refs):
            warnings.append(
                f"relationship {relationship.describe()}: "
                f"unknown table or column - ignored"
            )
            continue

        if relationship.key in seen:
            continue
        seen.add(relationship.key)
        kept.append(relationship)

    # Declared FKs come second so an authored edge keeps its note/cardinality.
    for relationship in _foreign_key_relationships(db_inspector):
        if relationship.key in seen:
            continue
        seen.add(relationship.key)
        kept.append(relationship)

    return kept


def _reconcile_examples(
    metadata: Metadata, db_inspector: DatabaseInspector, warnings: List[str]
) -> List[ExampleQuery]:
    """Drop examples whose SQL doesn't bind against the live schema.

    Applies the same additive-only guarantee as everything else here: an
    example is only ever a hint for how to answer, so one that references a
    dropped or renamed column must not survive to mislead the model - it's
    strictly worse than having no example for that shape of question.
    """
    if not metadata.examples:
        return []

    schema = build_schema(db_inspector)
    kept = []
    for example in metadata.examples:
        problems = find_schema_problems(example.sql, schema)
        if problems:
            warnings.append(
                f"example {example.question!r}: SQL doesn't match the schema - "
                f"ignored ({'; '.join(problems)})"
            )
            continue
        kept.append(example)
    return kept


def reconcile(
    metadata: Metadata, db_inspector: DatabaseInspector
) -> Tuple[Metadata, List[str]]:
    """Strip anything the database doesn't actually have, and add its own facts.

    This is where the additive-only guarantee is enforced: every reference in
    the file is checked against the live schema and dropped if it doesn't
    resolve, so nothing downstream has to wonder whether the metadata is true.
    Declared foreign keys are merged in, which is also what gives a database
    with no metadata file at all a usable join graph.

    Args:
        metadata: Freshly loaded metadata (may be empty)
        db_inspector: A connected inspector, ideally already profiled

    Returns:
        (reconciled metadata, human-readable warnings)
    """
    warnings: List[str] = []

    if metadata.version != SUPPORTED_VERSION:
        warnings.append(
            f"metadata version {metadata.version} is not supported "
            f"(expected {SUPPORTED_VERSION}) - reading it anyway"
        )

    real_tables = db_inspector.get_tables()
    columns_by_table = {
        table: {col["name"] for col in db_inspector.get_table_schema(table)}
        for table in real_tables
    }

    kept_tables: Dict[str, TableMeta] = {}
    for table, table_meta in metadata.tables.items():
        if table not in columns_by_table:
            warnings.append(f"table '{table}': no such table - ignored")
            continue
        kept_tables[table] = table_meta.model_copy(
            update={
                "columns": _reconcile_columns(
                    db_inspector, table, table_meta, columns_by_table[table], warnings
                )
            }
        )

    relationships = _reconcile_relationships(
        metadata, db_inspector, columns_by_table, warnings
    )
    examples = _reconcile_examples(metadata, db_inspector, warnings)

    reconciled = metadata.model_copy(
        update={
            "tables": kept_tables,
            "relationships": relationships,
            "examples": examples,
        }
    )

    for warning in warnings:
        logger.warning(f"Metadata: {warning}")
    return reconciled, warnings


def resolve_metadata(
    db_inspector: DatabaseInspector,
    source_path: Optional[Path] = None,
    override: Optional[Path] = None,
    use_file: bool = True,
) -> Tuple[Metadata, List[str]]:
    """Load, reconcile and return the metadata for a database. Never raises.

    The single entry point both the CLI and the web session use. Every failure
    becomes a warning, because losing the semantic layer should degrade the
    prompt, not the session.

    Args:
        db_inspector: A connected inspector, ideally already profiled
        source_path: The file the user opened, used for sidecar discovery
            (the workbook, not the temporary database, for Excel input)
        override: An explicit metadata file path, bypassing discovery
        use_file: When False, skip the sidecar entirely. Foreign keys are still
            read - those are facts from the database, not curated metadata.

    Returns:
        (reconciled metadata, human-readable warnings)
    """
    metadata = Metadata()
    warnings: List[str] = []

    path: Optional[Path] = None
    if use_file:
        if override is not None:
            path = override
            if not path.is_file():
                warnings.append(f"metadata file not found: {path}")
                path = None
        elif source_path is not None:
            path = discover(source_path)

    if path is not None:
        try:
            metadata = load(path)
            logger.info(f"Loaded metadata from {path}")
        except MetadataError as e:
            warnings.append(str(e))
            logger.warning(f"Metadata: {e}")
            metadata = Metadata()

    reconciled, reconcile_warnings = reconcile(metadata, db_inspector)
    return reconciled, warnings + reconcile_warnings


# --------------------------------------------------------------------------- #
# Scaffolding a new file
# --------------------------------------------------------------------------- #

#: Values at or below this length are treated as codes worth documenting. It
#: separates 'CA' and 'S' - which mean nothing without a dictionary - from
#: 'San Francisco', which needs no explanation.
CODE_VALUE_MAX_LENGTH = 4

#: Cap on codes pre-filled into a scaffold's `values:` block, so a column with
#: fifty categories doesn't bury the rest of the file.
SCAFFOLD_MAX_VALUES = 20


def _yaml_scalar(value: Optional[str]) -> str:
    """Render a string as a double-quoted YAML scalar, always safely.

    Hand-rolled rather than `yaml.safe_dump`ed because the scaffold's value is
    largely in its comments, and safe_dump strips those - and pulling in a
    round-tripping YAML library for one fixed-shape document isn't worth it.
    """
    text = (value or "").replace("\\", "\\\\").replace('"', '\\"')
    text = text.replace("\n", " ").strip()
    return f'"{text}"'


def _scaffold_values_block(
    codes: List[str], drafted: Dict[str, str], indent: str
) -> List[str]:
    """Pre-fill a `values:` block with the real codes, awaiting their meanings."""
    shown = codes[:SCAFFOLD_MAX_VALUES]
    lines = [f"{indent}values:"]
    for code in shown:
        lines.append(f"{indent}  {_yaml_scalar(code)}: {_yaml_scalar(drafted.get(code))}")
    if len(codes) > len(shown):
        lines.append(f"{indent}  # ... and {len(codes) - len(shown)} more value(s) in the data")
    return lines


def build_scaffold(
    db_inspector: DatabaseInspector,
    source_name: str,
    drafts: Optional[Dict[str, Dict[str, Any]]] = None,
) -> str:
    """Generate a starter metadata file for a database, as YAML text.

    Nobody hand-writes several hundred lines of YAML from a blank page, so the
    structure and the parts that can be read from the database - every table,
    every column, and the actual codes in each categorical column - are filled
    in already, leaving the meanings to be typed in.

    A freshly generated scaffold loads with zero warnings: blanks are empty
    strings, and anything speculative is emitted as a comment.

    Args:
        db_inspector: A connected, ideally profiled inspector
        source_name: Name of the file being documented, for the header comment
        drafts: Optional per-table LLM drafts from LLMQueryTranslator.draft_metadata

    Returns:
        The YAML document
    """
    drafts = drafts or {}
    lines = [
        f"# Metadata for {source_name}",
        "#",
        "# Everything here is optional and additive. It adds meaning on top of the",
        "# real schema and can never override it: types and keys always come from the",
        "# database, and a `values:` entry only labels a code that already exists.",
        "# Anything naming a table, column or value that isn't in the database is",
        "# ignored with a warning at startup - run the `metadata` command to see them.",
        "#",
        "# Fill in what matters and delete the rest. Empty entries are ignored.",
        "",
        f"version: {SUPPORTED_VERSION}",
        "",
        "database:",
        "  description: \"\"",
        "  conventions: {}",
        "    # How values are stored, when it isn't obvious from the type:",
        "    # currency: \"amounts are in minor units (cents) - divide by 100\"",
        "    # dates: \"ISO-8601 strings in TEXT columns\"",
        "  glossary: []",
        "    # Terms the user will say that the schema doesn't define:",
        "    # - term: \"active customer\"",
        "    #   means: \"placed an order in the trailing 12 months\"",
        "  rules: []",
        "    # Instructions applied to every generated query:",
        "    # - \"Exclude rows where is_test = 1 unless the user asks for test data.\"",
        "",
        "tables:",
        # (examples: goes after tables: below, so it reads after the schema it
        # refers to - see the block appended at the end of this scaffold.)
    ]

    for table in db_inspector.get_tables():
        draft = drafts.get(table, {})
        draft_columns = draft.get("columns") or {}
        column_profiles = db_inspector.profile.get(table, {})

        lines.append(f"  {table}:")
        lines.append(f"    description: {_yaml_scalar(draft.get('description'))}")
        lines.append(f"    grain: {_yaml_scalar(draft.get('grain'))}")
        synonyms = draft.get("synonyms") or []
        if isinstance(synonyms, list) and synonyms:
            rendered = ", ".join(_yaml_scalar(str(s)) for s in synonyms[:3])
            lines.append(f"    synonyms: [{rendered}]")
        else:
            lines.append("    synonyms: []")
        lines.append("    columns:")

        for col in db_inspector.get_table_schema(table):
            name = col["name"]
            column_draft = draft_columns.get(name) or {}
            if not isinstance(column_draft, dict):
                column_draft = {}

            lines.append(f"      {name}:")
            lines.append(
                f"        description: {_yaml_scalar(column_draft.get('description'))}"
            )

            profile = column_profiles.get(name)
            drafted_values = column_draft.get("values")
            drafted_values = (
                {str(k): str(v) for k, v in drafted_values.items()}
                if isinstance(drafted_values, dict)
                else {}
            )
            if profile and profile.get("type") == "enum":
                codes = [str(v) for v in profile["values"]]
                looks_coded = all(len(c) <= CODE_VALUE_MAX_LENGTH for c in codes)
                if codes and (looks_coded or drafted_values):
                    lines += _scaffold_values_block(codes, drafted_values, "        ")

    lines += [
        "",
        "relationships:",
        "  # Foreign keys declared in the database are read automatically, so list",
        "  # only the joins it doesn't declare - which for a spreadsheet is all of",
        "  # them. Both sides must be real columns, in table.column form:",
        "  #",
        "  # - from: orders.customer_id",
        "  #   to: customers.id",
        "  #   cardinality: many_to_one",
        "",
        "examples: []",
        "  # Curated question -> SQL pairs, shown to the model as demonstrations",
        "  # rather than described in prose - the single biggest quality lever for",
        "  # a small local model. Anything whose SQL doesn't match the schema is",
        "  # dropped at load time, same as everything else here:",
        "  #",
        "  # - question: how many orders shipped last month",
        "  #   sql: |",
        "  #     SELECT COUNT(*) FROM orders",
        "  #     WHERE status = 'shipped'",
        "  #       AND order_date >= date('now', 'start of month', '-1 month')",
        "  #       AND order_date <  date('now', 'start of month')",
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Lookup used by the query corrector
# --------------------------------------------------------------------------- #

def _codes_matching(values: Dict[str, ColumnValue], needle: str) -> List[str]:
    """Return every code whose label or a synonym equals `needle`, lowercased."""
    matches = []
    for code, meaning in values.items():
        if meaning.label.strip().lower() == needle or any(
            synonym.strip().lower() == needle for synonym in meaning.synonyms
        ):
            matches.append(code)
    return matches


def resolve_label(
    metadata: Optional[Metadata], table: str, column: str, value: str
) -> Optional[str]:
    """Translate a human label into the code stored in the column.

    Lets a model's plausible-but-wrong ``status = 'shipped'`` be repaired to
    ``status = 'S'`` deterministically. Without this, the value corrector can
    only offer the user a list of opaque codes to choose between - which needs
    the very knowledge the metadata file exists to record.

    Resolves only when exactly one code matches. A label or synonym claimed by
    two codes is genuinely ambiguous, and picking one would silently rewrite the
    query to a value that is valid - so nothing downstream would flag it - but
    quite possibly not what was meant. Better to leave the literal alone and let
    the existing value-mismatch path ask.

    Args:
        metadata: Reconciled metadata, or None
        table: Table the column belongs to
        column: Column being filtered
        value: The literal the LLM guessed

    Returns:
        The real code, or None if the value matches no label or several
    """
    if metadata is None:
        return None

    values = metadata.column(table, column).values
    if not values:
        return None

    matches = _codes_matching(values, value.strip().lower())
    if len(matches) == 1:
        return matches[0]
    if matches:
        logger.debug(
            f"Not resolving {table}.{column} = {value!r}: ambiguous between "
            f"{', '.join(repr(m) for m in matches)}"
        )
    return None

# Made with Bob
