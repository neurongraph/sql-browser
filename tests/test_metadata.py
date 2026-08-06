"""Tests for the curated metadata layer.

The emphasis is on the additive-only guarantee - that nothing in a metadata file
can make the tool assert something the database doesn't support - because that
property is what lets every other component trust the metadata without checking
it again.
"""

import pytest

from sql_browser import metadata as md
from sql_browser.query_corrector import find_value_mismatches


def write(path, text):
    """Write a metadata file, stripping the leading indentation of a literal."""
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #

def test_discover_prefers_stem_named_sidecar(tmp_path):
    source = tmp_path / "shop.db"
    source.touch()
    write(tmp_path / "sql-browser.meta.yaml", "version: 1\n")
    stem_named = write(tmp_path / "shop.meta.yaml", "version: 1\n")

    assert md.discover(source) == stem_named


def test_discover_falls_back_to_shared_name(tmp_path):
    source = tmp_path / "shop.db"
    source.touch()
    shared = write(tmp_path / "sql-browser.meta.yaml", "version: 1\n")

    assert md.discover(source) == shared


def test_discover_returns_none_when_absent(tmp_path):
    source = tmp_path / "shop.db"
    source.touch()

    assert md.discover(source) is None


def test_discover_uses_workbook_not_temporary_database(tmp_path):
    """Excel input is converted to a temp .db; the metadata sits by the .xlsx."""
    workbook = tmp_path / "book.xlsx"
    workbook.touch()
    sidecar = write(tmp_path / "book.meta.yaml", "version: 1\n")

    assert md.discover(workbook) == sidecar


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #

def test_load_empty_file_is_not_an_error(tmp_path):
    path = write(tmp_path / "empty.meta.yaml", "")

    assert md.load(path).is_empty


def test_load_rejects_non_mapping(tmp_path):
    path = write(tmp_path / "list.meta.yaml", "- one\n- two\n")

    with pytest.raises(md.MetadataError):
        md.load(path)


def test_load_rejects_malformed_yaml(tmp_path):
    path = write(tmp_path / "bad.meta.yaml", "tables: [unclosed\n")

    with pytest.raises(md.MetadataError):
        md.load(path)


def test_keys_with_only_comments_load_as_empty(tmp_path):
    """A half-filled-in file - the normal state of one - must still load."""
    path = write(
        tmp_path / "partial.meta.yaml",
        """
version: 1
database:
  rules:
    # - "not written yet"
tables:
relationships:
""",
    )

    loaded = md.load(path)
    assert loaded.database.rules == []
    assert loaded.tables == {}
    assert loaded.relationships == []


def test_value_shorthand_and_blank_labels(tmp_path):
    """`P: Pending` is accepted; a scaffold's unfilled `S: ""` is dropped."""
    path = write(
        tmp_path / "v.meta.yaml",
        """
version: 1
tables:
  orders:
    columns:
      status:
        values:
          P: Pending
          S: ""
""",
    )

    values = md.load(path).column("orders", "status").values
    assert values["P"].label == "Pending"
    assert "S" not in values


# --------------------------------------------------------------------------- #
# Reconciliation: the additive-only guarantee
# --------------------------------------------------------------------------- #

def test_reconcile_drops_unknown_table(inspector):
    metadata = md.Metadata.model_validate(
        {"tables": {"ghosts": {"description": "not real"}}}
    )

    reconciled, warnings = md.reconcile(metadata, inspector)

    assert "ghosts" not in reconciled.tables
    assert any("ghosts" in w for w in warnings)


def test_reconcile_drops_unknown_column_but_keeps_the_table(inspector):
    metadata = md.Metadata.model_validate(
        {
            "tables": {
                "orders": {
                    "description": "orders",
                    "columns": {
                        "status": {"description": "state"},
                        "nonexistent": {"description": "nope"},
                    },
                }
            }
        }
    )

    reconciled, warnings = md.reconcile(metadata, inspector)

    assert reconciled.table("orders").description == "orders"
    assert "status" in reconciled.table("orders").columns
    assert "nonexistent" not in reconciled.table("orders").columns
    assert any("nonexistent" in w for w in warnings)


def test_reconcile_drops_values_absent_from_the_data(inspector):
    """The data decides which codes exist; the file only names them."""
    metadata = md.Metadata.model_validate(
        {
            "tables": {
                "orders": {
                    "columns": {
                        "status": {
                            "values": {
                                "P": {"label": "Pending"},
                                "Z": {"label": "Invented"},
                            }
                        }
                    }
                }
            }
        }
    )

    reconciled, warnings = md.reconcile(metadata, inspector)

    values = reconciled.column("orders", "status").values
    assert set(values) == {"P"}
    assert any("'Z'" in w for w in warnings)


def test_reconcile_drops_relationship_with_unknown_endpoint(inspector):
    metadata = md.Metadata.model_validate(
        {"relationships": [{"from": "orders.customer_id", "to": "ghosts.id"}]}
    )

    reconciled, warnings = md.reconcile(metadata, inspector)

    assert all(r.to != "ghosts.id" for r in reconciled.relationships)
    assert any("ghosts.id" in w for w in warnings)


def test_reconcile_adds_declared_foreign_keys(inspector):
    """A database with real constraints gets a join graph with no file at all."""
    reconciled, warnings = md.reconcile(md.Metadata(), inspector)

    edges = {(r.from_, r.to) for r in reconciled.relationships}
    assert ("orders.customer_id", "customers.id") in edges
    assert all(r.declared for r in reconciled.relationships)
    assert warnings == []


def test_authored_relationship_wins_over_the_declared_duplicate(inspector):
    """The authored edge carries a note worth keeping; the FK adds nothing."""
    metadata = md.Metadata.model_validate(
        {
            "relationships": [
                {
                    "from": "orders.customer_id",
                    "to": "customers.id",
                    "note": "one customer, many orders",
                }
            ]
        }
    )

    reconciled, _ = md.reconcile(metadata, inspector)

    matching = [
        r for r in reconciled.relationships
        if (r.from_, r.to) == ("orders.customer_id", "customers.id")
    ]
    assert len(matching) == 1
    assert matching[0].note == "one customer, many orders"
    assert matching[0].declared is False


def test_unsupported_version_warns_but_still_loads(inspector):
    metadata = md.Metadata.model_validate(
        {"version": 99, "database": {"description": "from the future"}}
    )

    reconciled, warnings = md.reconcile(metadata, inspector)

    assert reconciled.database.description == "from the future"
    assert any("version" in w for w in warnings)


# --------------------------------------------------------------------------- #
# resolve_metadata: the entry point that must never raise
# --------------------------------------------------------------------------- #

def test_resolve_metadata_survives_a_malformed_file(inspector, db_path):
    write(db_path.with_name("shop.meta.yaml"), "tables: [unclosed\n")

    metadata, warnings = md.resolve_metadata(inspector, source_path=db_path)

    # The session still gets usable metadata - the foreign keys - plus a warning.
    assert warnings
    assert any(r.declared for r in metadata.relationships)


def test_resolve_metadata_reports_a_missing_override(inspector, tmp_path):
    metadata, warnings = md.resolve_metadata(
        inspector, override=tmp_path / "nope.meta.yaml"
    )

    assert any("not found" in w for w in warnings)
    assert metadata.relationships  # foreign keys are still read


def test_use_file_false_skips_the_sidecar_but_keeps_foreign_keys(inspector, db_path):
    write(
        db_path.with_name("shop.meta.yaml"),
        'version: 1\ndatabase:\n  description: "should be ignored"\n',
    )

    metadata, warnings = md.resolve_metadata(
        inspector, source_path=db_path, use_file=False
    )

    assert metadata.database.description is None
    assert metadata.relationships
    assert warnings == []


# --------------------------------------------------------------------------- #
# Composite foreign keys
# --------------------------------------------------------------------------- #

def test_composite_foreign_key_is_one_constraint(composite_inspector):
    """Both columns of the key must stay on a single edge, in declaration order."""
    keys = composite_inspector.get_foreign_keys("offices")

    assert keys == [
        {"table": "regions", "columns": [("country", "country"), ("region_code", "code")]}
    ]


def test_composite_key_resolves_an_implicit_parent_key(composite_inspector):
    """`REFERENCES regions` pairs each column with the parent PK at that position."""
    keys = composite_inspector.get_foreign_keys("depots")

    assert keys == [
        {"table": "regions", "columns": [("c", "country"), ("rc", "code")]}
    ]


def test_single_column_implicit_reference(composite_inspector):
    assert composite_inspector.get_foreign_keys("staff") == [
        {"table": "offices", "columns": [("office_id", "id")]}
    ]


def test_reference_to_a_parent_without_a_primary_key_is_dropped(composite_inspector):
    """Better no edge than half of one - a guessed join is worse than none."""
    assert composite_inspector.get_foreign_keys("orphans") == []


def test_composite_edge_renders_as_a_single_join(composite_inspector):
    reconciled, warnings = md.reconcile(md.Metadata(), composite_inspector)

    composite = [r for r in reconciled.relationships if r.is_composite]
    assert len(composite) == 2  # offices and depots
    described = {r.describe() for r in composite}
    assert "offices.(country, region_code) -> regions.(country, code)" in described
    assert warnings == []


def test_authored_composite_relationship_accepts_a_list(composite_inspector):
    """`from: [a, b]` is as valid as `from: "a, b"` - both are natural in YAML."""
    metadata = md.Metadata.model_validate(
        {
            "relationships": [
                {
                    "from": ["offices.country", "offices.region_code"],
                    "to": ["regions.country", "regions.code"],
                }
            ]
        }
    )

    reconciled, warnings = md.reconcile(metadata, composite_inspector)

    authored = [r for r in reconciled.relationships if not r.declared]
    assert len(authored) == 1
    assert authored[0].is_composite
    assert authored[0].from_refs == ["offices.country", "offices.region_code"]
    assert warnings == []


def test_mismatched_composite_column_counts_are_rejected(composite_inspector):
    """Two columns on one side and one on the other describes no usable join."""
    metadata = md.Metadata.model_validate(
        {
            "relationships": [
                {"from": "offices.country, offices.region_code", "to": "regions.code"}
            ]
        }
    )

    reconciled, warnings = md.reconcile(metadata, composite_inspector)

    assert all(r.declared for r in reconciled.relationships)
    assert any("column(s) on the left" in w for w in warnings)


# --------------------------------------------------------------------------- #
# resolve_label and the value corrector
# --------------------------------------------------------------------------- #

@pytest.fixture
def labelled_inspector(inspector):
    """The fixture inspector with a reconciled status dictionary attached."""
    metadata = md.Metadata.model_validate(
        {
            "tables": {
                "orders": {
                    "columns": {
                        "status": {
                            "values": {
                                "P": {"label": "Pending"},
                                "S": {"label": "Shipped", "synonyms": ["dispatched"]},
                                "C": {"label": "Cancelled"},
                            }
                        }
                    }
                }
            }
        }
    )
    inspector.metadata, inspector.metadata_warnings = md.reconcile(metadata, inspector)
    return inspector


@pytest.mark.parametrize(
    "value,expected",
    [
        ("Shipped", "S"),
        ("shipped", "S"),
        ("  SHIPPED  ", "S"),
        ("dispatched", "S"),
        ("Cancelled", "C"),
        ("nonsense", None),
    ],
)
def test_resolve_label(labelled_inspector, value, expected):
    assert (
        md.resolve_label(labelled_inspector.metadata, "orders", "status", value)
        == expected
    )


def test_resolve_label_without_metadata_is_none(inspector):
    assert md.resolve_label(None, "orders", "status", "Shipped") is None


def test_corrector_rewrites_a_label_to_its_code(labelled_inspector):
    """The payoff: 'shipped' becomes 'S' with nothing to ask the user."""
    sql, mismatches = find_value_mismatches(
        "SELECT * FROM orders WHERE status = 'shipped'", labelled_inspector
    )

    assert "'S'" in sql
    assert "shipped" not in sql
    assert mismatches == []


def test_corrector_rewrites_a_synonym(labelled_inspector):
    sql, mismatches = find_value_mismatches(
        "SELECT * FROM orders WHERE status = 'dispatched'", labelled_inspector
    )

    assert "'S'" in sql
    assert mismatches == []


def test_corrector_leaves_a_real_code_alone(labelled_inspector):
    sql, mismatches = find_value_mismatches(
        "SELECT * FROM orders WHERE status = 'S'", labelled_inspector
    )

    assert "'S'" in sql
    assert mismatches == []


def test_corrector_still_asks_when_no_label_matches(labelled_inspector):
    """An unrecognisable value falls through to the existing interactive path."""
    _, mismatches = find_value_mismatches(
        "SELECT * FROM orders WHERE status = 'quantum'", labelled_inspector
    )

    assert len(mismatches) == 1
    assert mismatches[0]["attempted_value"] == "quantum"


def test_ambiguous_synonym_is_reported_and_not_resolved(inspector):
    """Two codes claiming one phrase can't be resolved, so it must not be guessed.

    Silently picking one would rewrite the query to a value that is *valid* -
    so nothing downstream flags it - but quite possibly not what was meant.
    """
    metadata = md.Metadata.model_validate(
        {
            "tables": {
                "orders": {
                    "columns": {
                        "status": {
                            "values": {
                                "P": {"label": "Pending", "synonyms": ["done"]},
                                "S": {"label": "Shipped", "synonyms": ["done"]},
                            }
                        }
                    }
                }
            }
        }
    )

    reconciled, warnings = md.reconcile(metadata, inspector)

    assert any("'done' is claimed by" in w for w in warnings)
    assert md.resolve_label(reconciled, "orders", "status", "done") is None
    # The unambiguous labels still work.
    assert md.resolve_label(reconciled, "orders", "status", "Shipped") == "S"


def test_ambiguous_value_falls_through_to_the_picker(inspector):
    metadata = md.Metadata.model_validate(
        {
            "tables": {
                "orders": {
                    "columns": {
                        "status": {
                            "values": {
                                "P": {"label": "Pending", "synonyms": ["done"]},
                                "S": {"label": "Shipped", "synonyms": ["done"]},
                            }
                        }
                    }
                }
            }
        }
    )
    inspector.metadata, inspector.metadata_warnings = md.reconcile(metadata, inspector)

    sql, mismatches = find_value_mismatches(
        "SELECT * FROM orders WHERE status = 'done'", inspector
    )

    assert "'done'" in sql  # left alone, not silently rewritten
    assert len(mismatches) == 1


def test_label_colliding_with_another_code_is_reported(inspector):
    """`'P'` labelled "S" while `'S'` exists is confusing enough to flag."""
    metadata = md.Metadata.model_validate(
        {
            "tables": {
                "orders": {
                    "columns": {
                        "status": {
                            "values": {"P": {"label": "S"}, "S": {"label": "Shipped"}}
                        }
                    }
                }
            }
        }
    )

    _, warnings = md.reconcile(metadata, inspector)

    assert any("is also the code" in w for w in warnings)


def test_a_single_value_needs_no_ambiguity_check(inspector):
    """One documented code can't collide with anything - no spurious warnings."""
    metadata = md.Metadata.model_validate(
        {
            "tables": {
                "orders": {"columns": {"status": {"values": {"S": {"label": "S"}}}}}
            }
        }
    )

    _, warnings = md.reconcile(metadata, inspector)

    assert warnings == []


def test_corrector_without_metadata_behaves_as_before(inspector):
    """No metadata: the label is unresolvable, so it's offered as a mismatch."""
    _, mismatches = find_value_mismatches(
        "SELECT * FROM orders WHERE status = 'shipped'", inspector
    )

    assert len(mismatches) == 1
    assert sorted(mismatches[0]["candidates"]) == ["C", "P", "S"]


# --------------------------------------------------------------------------- #
# Scaffolding
# --------------------------------------------------------------------------- #

def test_scaffold_round_trips_without_warnings(inspector, tmp_path):
    """A generated scaffold must load clean, or every session starts noisy."""
    path = write(
        tmp_path / "generated.meta.yaml", md.build_scaffold(inspector, "shop.db")
    )

    reconciled, warnings = md.reconcile(md.load(path), inspector)

    assert warnings == []
    assert set(reconciled.tables) == {"customers", "orders"}


def test_scaffold_prefills_coded_values_only(inspector, tmp_path):
    """Short codes need a dictionary; readable values don't."""
    text = md.build_scaffold(inspector, "shop.db")

    status_block = text.split("status:")[1]
    assert "values:" in status_block.split("total_cents:")[0]
    # `name` holds 'Ada'/'Grace' - self-explanatory, so no dictionary is offered.
    name_block = text.split("      name:")[1].split("      region:")[0]
    assert "values:" not in name_block


def test_scaffold_includes_every_table_and_column(inspector):
    text = md.build_scaffold(inspector, "shop.db")

    for token in ("customers:", "orders:", "customer_id:", "total_cents:", "region:"):
        assert token in text

# Made with Bob
