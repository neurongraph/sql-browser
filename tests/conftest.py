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

# Made with Bob
