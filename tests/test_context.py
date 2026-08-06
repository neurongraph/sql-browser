"""Tests for the prompt text handed to the LLM.

The rendered string *is* the interface to the model, so these assert on what
does and doesn't appear in it - particularly that a metadata entry which failed
reconciliation leaves no trace, which is the visible half of the additive-only
guarantee.
"""

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

# Made with Bob
