"""Tests for the prompt text handed to the LLM.

The rendered string *is* the interface to the model, so these assert on what
does and doesn't appear in it - particularly that a metadata entry which failed
reconciliation leaves no trace, which is the visible half of the additive-only
guarantee.
"""

from sql_browser import context
from sql_browser import metadata as md
from sql_browser.context import render_schema_context


def attach(inspector, raw):
    """Reconcile a raw metadata dict onto an inspector, as a session would."""
    reconciled, warnings = md.reconcile(md.Metadata.model_validate(raw), inspector)
    inspector.metadata, inspector.metadata_warnings = reconciled, warnings
    return inspector


def test_renders_schema_without_metadata(inspector):
    text = render_schema_context(inspector)

    assert "Database Schema:" in text
    assert "Table: orders (3 rows)" in text
    assert "  - id: INTEGER [PRIMARY KEY]" in text
    # No metadata file, so none of the curated sections appear.
    for heading in ("DATABASE", "CONVENTIONS", "RULES", "GLOSSARY"):
        assert heading not in text


def test_declared_foreign_keys_reach_the_prompt(inspector):
    """The join graph is the one thing a model can't guess - it must be there."""
    attach(inspector, {})

    text = render_schema_context(inspector)

    assert "RELATIONSHIPS" in text
    assert "orders.customer_id -> customers.id" in text


def test_renders_the_narrative_layer(inspector):
    attach(
        inspector,
        {
            "database": {
                "description": "Toy shop data.",
                "conventions": {"currency": "amounts are in cents"},
                "glossary": [{"term": "big order", "means": "over 2000 cents"}],
                "rules": ["Never include cancelled orders unless asked."],
            },
            "tables": {
                "orders": {
                    "description": "One row per order.",
                    "grain": "order",
                    "synonyms": ["purchases"],
                }
            },
        },
    )

    text = render_schema_context(inspector)

    assert "Toy shop data." in text
    assert "currency: amounts are in cents" in text
    assert "big order: over 2000 cents" in text
    assert "1. Never include cancelled orders unless asked." in text
    assert "One row per order." in text
    assert "Grain: one row per order" in text
    assert "Also called: purchases" in text


def test_renders_value_labels_against_real_codes(inspector):
    attach(
        inspector,
        {
            "tables": {
                "orders": {
                    "columns": {
                        "status": {
                            "description": "Fulfilment state",
                            "values": {
                                "P": {"label": "Pending"},
                                "S": {"label": "Shipped", "synonyms": ["dispatched"]},
                                "C": {"label": "Cancelled"},
                            },
                        }
                    }
                }
            }
        },
    )

    text = render_schema_context(inspector)

    assert "Fulfilment state" in text
    assert "'S'=Shipped [aka dispatched]" in text
    assert "'P'=Pending" in text


def test_dropped_entries_leave_no_trace_in_the_prompt(inspector):
    """A stale file must not put anything untrue in front of the model."""
    attach(
        inspector,
        {
            "tables": {
                "ghosts": {"description": "invented table"},
                "orders": {
                    "columns": {
                        "nonexistent": {"description": "invented column"},
                        "status": {
                            "values": {
                                "P": {"label": "Pending"},
                                "Z": {"label": "Invented code"},
                            }
                        },
                    }
                },
            },
            "relationships": [{"from": "orders.customer_id", "to": "ghosts.id"}],
        },
    )

    text = render_schema_context(inspector)

    assert "ghosts" not in text
    assert "invented table" not in text
    assert "invented column" not in text
    assert "Invented code" not in text
    assert "'Z'" not in text
    # The real parts survive.
    assert "'P'=Pending" in text
    assert inspector.metadata_warnings


def test_column_annotations_render(inspector):
    attach(
        inspector,
        {
            "tables": {
                "orders": {
                    "columns": {
                        "total_cents": {"semantic_type": "money", "unit": "EUR_minor"},
                        "customer_id": {"synonyms": ["client"]},
                    }
                },
                "customers": {"columns": {"name": {"pii": True}}},
            }
        },
    )

    text = render_schema_context(inspector)

    assert "money in EUR_minor" in text
    assert "also called client" in text
    assert "PII" in text


def test_composite_key_is_rendered_as_one_join_with_a_warning(composite_inspector):
    """The model must be told to use every column, or the join fans out."""
    composite_inspector.metadata, _ = md.reconcile(md.Metadata(), composite_inspector)

    text = render_schema_context(composite_inspector)

    assert "offices.(country, region_code) -> regions.(country, code)" in text
    assert "composite key: join on ALL of these columns together" in text
    # The columns must never appear as two independent single-column paths.
    assert "offices.country -> regions.country" not in text
    assert "offices.region_code -> regions.code" not in text


def test_unprofiled_database_renders_plain_columns(db_path):
    """No profile: the listing is types only, exactly as it always was."""
    from sql_browser.database import DatabaseInspector

    inspector = DatabaseInspector(str(db_path))
    inspector.connect()
    try:
        text = render_schema_context(inspector)
    finally:
        inspector.disconnect()

    assert "  - status: TEXT" in text
    assert "(values:" not in text


# --------------------------------------------------------------------------- #
# Relevance-based pruning (BACKLOG.md #1)
# --------------------------------------------------------------------------- #

#: The brief renderer's column line ("  Columns: a, b, c") vs. the full
#: renderer's heading ("Columns:" alone, followed by one line per column) -
#: this substring only ever appears in the brief form.
BRIEF_COLUMNS_MARKER = "  Columns: "


def test_no_question_renders_every_table_in_full(inspector):
    """The `question=None` default must be identical to the old behaviour."""
    no_question_result = render_schema_context(inspector)
    explicit_none_result = render_schema_context(inspector, question=None)

    assert no_question_result == explicit_none_result
    assert "  - status: TEXT" in no_question_result
    assert BRIEF_COLUMNS_MARKER not in no_question_result


def test_relevant_tables_expands_one_hop_and_prunes_the_rest(composite_inspector):
    """A table two hops from anything matched is pruned; one hop away is kept."""
    composite_inspector.metadata, _ = md.reconcile(md.Metadata(), composite_inspector)

    text = render_schema_context(composite_inspector, question="how many staff are there")

    # staff: directly matched, full detail.
    assert "Table: staff (0 rows)" in text
    assert "  - office_id: INTEGER" in text
    # offices: one hop from staff (staff.office_id -> offices.id), full detail.
    assert "  - country: TEXT\n" in text
    assert "  - region_code: TEXT" in text
    # regions: two hops away (offices -> regions), pruned to a brief line -
    # its full per-column rendering (with the PRIMARY KEY marker) must be gone.
    assert "Columns: country, code, name" in text
    assert "  - country: TEXT [PRIMARY KEY]" not in text
    # depots and nokey/orphans are unrelated to staff entirely - also pruned.
    assert "Columns: id, c, rc" in text
    assert "Columns: a, b" in text


def test_relevant_tables_falls_back_to_everything_when_nothing_matches(composite_inspector):
    """Scoring that can't narrow anything down must not hide tables."""
    composite_inspector.metadata, _ = md.reconcile(md.Metadata(), composite_inspector)

    text = render_schema_context(composite_inspector, question="xyzzy plugh quux")

    # Every table renders in full, exactly as with no question at all.
    assert "  - country: TEXT [PRIMARY KEY]" in text
    assert BRIEF_COLUMNS_MARKER not in text


def test_relevant_tables_matches_via_value_synonym(inspector):
    """A question naming a value pulls in the table that value lives on."""
    attach(
        inspector,
        {
            "tables": {
                "orders": {
                    "columns": {
                        "status": {
                            "values": {"S": {"label": "Shipped", "synonyms": ["in transit"]}}
                        }
                    }
                }
            }
        },
    )

    relevant = context.relevant_tables(
        "which orders are in transit", inspector.get_full_schema(), inspector.metadata
    )

    # "in transit" only matches via the status value's synonym, on orders.
    # customers is pulled in too, one hop away via the declared foreign key.
    assert relevant == {"orders", "customers"}


def test_relevant_tables_singular_plural_matches_table_name(inspector):
    attach(inspector, {})

    relevant = context.relevant_tables(
        "how many customer records exist",
        inspector.get_full_schema(),
        inspector.metadata,
    )

    assert "customers" in relevant


# --------------------------------------------------------------------------- #
# Example selection (BACKLOG.md #2)
# --------------------------------------------------------------------------- #

def test_select_examples_ranks_by_token_overlap():
    examples = [
        md.ExampleQuery(question="total revenue by customer", sql="SELECT 2"),
        md.ExampleQuery(question="how many orders shipped last month", sql="SELECT 1"),
    ]

    selected = context.select_examples("how many orders were shipped", examples)

    assert selected
    assert selected[0]["sql"] == "SELECT 1"


def test_select_examples_returns_nothing_when_no_overlap():
    examples = [md.ExampleQuery(question="total revenue by customer", sql="SELECT 2")]

    assert context.select_examples("what is the weather today", examples) == []


def test_select_examples_respects_the_limit():
    examples = [
        md.ExampleQuery(question=f"orders question {i}", sql=f"SELECT {i}")
        for i in range(5)
    ]

    selected = context.select_examples("orders", examples, limit=2)

    assert len(selected) == 2


def test_select_examples_with_no_examples_is_a_noop():
    assert context.select_examples("how many orders", []) == []


# --------------------------------------------------------------------------- #
# Prose-parse prompt construction (the interface to the parsing model)
# --------------------------------------------------------------------------- #

def test_schema_hint_for_table_uses_real_names_and_profiled_codes(inspector):
    hint = context.schema_hint_for_table(inspector, "orders")

    assert "Table: orders" in hint
    assert "use these exact names" in hint
    # status is categorical, so its actual codes are offered to the parser.
    assert "status" in hint
    assert "'P'" in hint and "'S'" in hint


def test_schema_hint_for_db_lists_every_table(inspector):
    hint = context.schema_hint_for_db(inspector)

    assert "orders(" in hint
    assert "customers(" in hint


def test_build_prose_parse_prompt_carries_section_prose_and_hint(inspector):
    hint = context.schema_hint_for_table(inspector, "orders")
    prompt = context.build_prose_parse_prompt(
        "table", "status: P means Pending", hint, table="orders"
    )

    assert "the table 'orders'" in prompt
    assert "status: P means Pending" in prompt  # the prose itself
    assert "Table: orders" in prompt  # the schema hint
    assert "JSON only" in prompt
    # The table contract's distinctive fields are present.
    assert '"grain"' in prompt


def test_build_prose_parse_prompt_sections_differ():
    """Each section gets its own JSON contract, not a generic one."""
    db = context.build_prose_parse_prompt("database", "notes", "hint")
    rel = context.build_prose_parse_prompt("relationships", "notes", "hint")

    assert '"conventions"' in db
    assert '"relationships"' in rel
    assert '"conventions"' not in rel


def test_metadata_sheet_classifier_prompt_includes_title_and_sample():
    prompt = context.build_metadata_sheet_classifier_prompt(
        "Notes", "BGC | Bank Giro Credit"
    )

    assert "Notes" in prompt
    assert "BGC | Bank Giro Credit" in prompt
    assert '"kind"' in prompt

# Made with Bob
