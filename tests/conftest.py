"""Shared fixtures: a small real SQLite database to reconcile metadata against.

A real database rather than a mock, because what's being tested is precisely
the agreement between a metadata file and a live schema - foreign keys read from
PRAGMA, columns that do and don't exist, values that are and aren't in the data.
Mocking that away would test the assertions instead of the behaviour.
"""

import sqlite3

import pytest

from sql_browser.database import DatabaseInspector
from sql_browser.profiler import profile_database


@pytest.fixture
def db_path(tmp_path):
    """Create a two-table database with a declared foreign key and coded values."""
    path = tmp_path / "shop.db"
    connection = sqlite3.connect(str(path))
    connection.executescript(
        """
        CREATE TABLE customers (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            region TEXT
        );
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY,
            customer_id INTEGER NOT NULL,
            status TEXT,
            total_cents INTEGER,
            FOREIGN KEY (customer_id) REFERENCES customers(id)
        );
        INSERT INTO customers (id, name, region) VALUES
            (1, 'Ada', 'EU'), (2, 'Grace', 'US');
        INSERT INTO orders (id, customer_id, status, total_cents) VALUES
            (1, 1, 'P', 1000),
            (2, 1, 'S', 2500),
            (3, 2, 'C', 700);
        """
    )
    connection.commit()
    connection.close()
    return path


@pytest.fixture
def inspector(db_path):
    """A connected, profiled inspector over the fixture database."""
    inspector = DatabaseInspector(str(db_path))
    inspector.connect()
    inspector.profile = profile_database(inspector)
    yield inspector
    inspector.disconnect()


@pytest.fixture
def composite_db_path(tmp_path):
    """A database exercising every shape of foreign key declaration.

    Composite keys are the interesting case: `PRAGMA foreign_key_list` reports
    one row per column, and treating those rows as independent edges would
    present a join on a single column of the key as valid - which fans out and
    returns wrong rows.
    """
    path = tmp_path / "composite.db"
    connection = sqlite3.connect(str(path))
    connection.executescript(
        """
        CREATE TABLE regions (
            country TEXT,
            code TEXT,
            name TEXT,
            PRIMARY KEY (country, code)
        );
        -- composite key, parent columns named explicitly
        CREATE TABLE offices (
            id INTEGER PRIMARY KEY,
            country TEXT,
            region_code TEXT,
            FOREIGN KEY (country, region_code) REFERENCES regions(country, code)
        );
        -- composite key targeting the parent's primary key implicitly
        CREATE TABLE depots (
            id INTEGER PRIMARY KEY,
            c TEXT,
            rc TEXT,
            FOREIGN KEY (c, rc) REFERENCES regions
        );
        -- single column, implicit parent key
        CREATE TABLE staff (
            id INTEGER PRIMARY KEY,
            office_id INTEGER REFERENCES offices
        );
        -- parent has no primary key at all, so the reference is unresolvable
        CREATE TABLE nokey (a TEXT, b TEXT);
        CREATE TABLE orphans (
            id INTEGER PRIMARY KEY,
            a TEXT,
            FOREIGN KEY (a) REFERENCES nokey
        );
        INSERT INTO regions VALUES ('US','CA','California'), ('FR','CA','Cantal');
        INSERT INTO offices VALUES (1,'US','CA');
        """
    )
    connection.commit()
    connection.close()
    return path


@pytest.fixture
def composite_inspector(composite_db_path):
    """A connected inspector over the composite-key database."""
    inspector = DatabaseInspector(str(composite_db_path))
    inspector.connect()
    yield inspector
    inspector.disconnect()

# Made with Bob
