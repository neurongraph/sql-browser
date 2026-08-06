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
    """A join edge between two columns, in `table.column` form."""

    model_config = ConfigDict(populate_by_name=True)

    #: `from` is a Python keyword, so the field is `from_` with a YAML alias.
    from_: str = Field(alias="from")
    to: str
    cardinality: Optional[str] = None
    note: Optional[str] = None
    #: True when this edge came from PRAGMA foreign_key_list rather than the file.
    declared: bool = False

    @property
    def from_parts(self) -> Tuple[str, str]:
        return _split_ref(self.from_)

    @property
    def to_parts(self) -> Tuple[str, str]:
        return _split_ref(self.to)

    @property
    def key(self) -> Tuple[str, str]:
        """Identity for de-duplicating file-declared against PRAGMA-derived edges."""
        return (self.from_, self.to)


class GlossaryTerm(_LenientModel):
    """A business term and what it means in this database."""

    term: str
    means: str


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
    """Build relationships from the database's own declared foreign keys."""
    relationships = []
    for table in db_inspector.get_tables():
        for fk in db_inspector.get_foreign_keys(table):
            relationships.append(
                Relationship(
                    from_=f"{table}.{fk['from']}",
                    to=f"{fk['table']}.{fk['to']}",
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

        kept[column] = column_meta

    return kept


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
        if not endpoint_exists(relationship.from_) or not endpoint_exists(relationship.to):
            warnings.append(
                f"relationship {relationship.from_} -> {relationship.to}: "
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

    reconciled = metadata.model_copy(
        update={"tables": kept_tables, "relationships": relationships}
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
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Lookup used by the query corrector
# --------------------------------------------------------------------------- #

def resolve_label(
    metadata: Optional[Metadata], table: str, column: str, value: str
) -> Optional[str]:
    """Translate a human label into the code stored in the column.

    Lets a model's plausible-but-wrong ``status = 'shipped'`` be repaired to
    ``status = 'S'`` deterministically. Without this, the value corrector can
    only offer the user a list of opaque codes to choose between - which needs
    the very knowledge the metadata file exists to record.

    Args:
        metadata: Reconciled metadata, or None
        table: Table the column belongs to
        column: Column being filtered
        value: The literal the LLM guessed

    Returns:
        The real code, or None if the value isn't a known label or synonym
    """
    if metadata is None:
        return None

    values = metadata.column(table, column).values
    if not values:
        return None

    needle = value.strip().lower()
    for code, meaning in values.items():
        if meaning.label.strip().lower() == needle:
            return code
        if any(synonym.strip().lower() == needle for synonym in meaning.synonyms):
            return code
    return None

# Made with Bob
