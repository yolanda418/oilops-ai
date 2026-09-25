"""STEP 7 - migrations tests.

We verify that the schema migration system is:

    * idempotent (safe to run multiple times)
    * additive (never drops existing data)
    * records its version in a `schema_version` table
    * adds paid_at and payment_note columns to invoices
    * never errors out on an already-migrated DB
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from database.db import get_connection, init_db
from database.migrations import (
    MIGRATIONS,
    apply_migrations,
    get_applied_version,
    INVOICES_PAID_AT_COLUMN,
    INVOICES_PAYMENT_NOTE_COLUMN,
    INVOICES_DOCUMENT_PATH_COLUMN,
    INVOICES_DOCUMENT_SHA256_COLUMN,
    INVOICES_DOCUMENT_SIZE_COLUMN,
)


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    """Use a fresh SQLite file per test."""
    db_path = tmp_path / "mig_test.db"
    monkeypatch.setenv("OILOPS_DB_PATH", str(db_path))
    init_db(str(db_path))
    yield str(db_path)


def _columns(conn):
    return [
        r[1]
        for r in conn.execute("PRAGMA table_info(invoices)").fetchall()
    ]


def test_migration_adds_paid_at_column(fresh_db):
    conn = get_connection(fresh_db)
    try:
        assert INVOICES_PAID_AT_COLUMN in _columns(conn)
        assert INVOICES_PAYMENT_NOTE_COLUMN in _columns(conn)
    finally:
        conn.close()


def test_migration_records_schema_version(fresh_db):
    conn = get_connection(fresh_db)
    try:
        assert get_applied_version(conn) >= 1
        # schema_version table exists.
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name = 'schema_version'"
        ).fetchall()
        assert len(rows) == 1
    finally:
        conn.close()


def test_migration_is_idempotent(fresh_db):
    """Re-running apply_migrations must be a no-op that returns []."""
    conn = get_connection(fresh_db)
    try:
        applied = apply_migrations(conn)
        assert applied == [], (
            "Re-running migrations on an already-migrated DB "
            "should return an empty list, got: %r" % (applied,)
        )
        version_after_first = get_applied_version(conn)
        applied2 = apply_migrations(conn)
        assert applied2 == []
        assert get_applied_version(conn) == version_after_first
    finally:
        conn.close()


def test_migration_is_idempotent_under_init_db(fresh_db):
    """init_db() runs apply_migrations() on every invocation. Calling
    it twice in a row must not raise and must keep all data."""
    conn = get_connection(fresh_db)
    try:
        conn.execute(
            "INSERT INTO invoices (vendor_name, total_amount, status) "
            "VALUES (?, ?, ?)",
            ("Acme", 100.0, "pending"),
        )
        conn.commit()
        before_count = conn.execute(
            "SELECT COUNT(*) FROM invoices"
        ).fetchone()[0]
    finally:
        conn.close()

    init_db(fresh_db)
    init_db(fresh_db)
    init_db(fresh_db)

    conn = get_connection(fresh_db)
    try:
        after_count = conn.execute(
            "SELECT COUNT(*) FROM invoices"
        ).fetchone()[0]
        assert after_count == before_count
    finally:
        conn.close()


def test_paid_at_column_is_nullable_text(fresh_db):
    conn = get_connection(fresh_db)
    try:
        # We can insert a row without providing paid_at / payment_note.
        cur = conn.execute(
            "INSERT INTO invoices (vendor_name, total_amount, status) "
            "VALUES (?, ?, ?)",
            ("Acme", 100.0, "pending"),
        )
        new_id = int(cur.lastrowid)
        row = conn.execute(
            "SELECT paid_at, payment_note FROM invoices WHERE id = ?",
            (new_id,),
        ).fetchone()
        assert row["paid_at"] is None
        assert row["payment_note"] is None
    finally:
        conn.close()


def test_migration_describes_columns_correctly(fresh_db):
    conn = get_connection(fresh_db)
    try:
        rows = conn.execute(
            "SELECT name, type, \"notnull\", dflt_value "
            "FROM pragma_table_info('invoices') "
            "WHERE name IN (?, ?)",
            (INVOICES_PAID_AT_COLUMN, INVOICES_PAYMENT_NOTE_COLUMN),
        ).fetchall()
        by_name = {r["name"]: r for r in rows}
        for col in (INVOICES_PAID_AT_COLUMN, INVOICES_PAYMENT_NOTE_COLUMN):
            assert col in by_name
            r = by_name[col]
            assert "TEXT" in r["type"].upper(), (
                "%s must be TEXT, got %s" % (col, r["type"])
            )
            assert r["notnull"] == 0, (
                "%s must be nullable" % col
            )
    finally:
        conn.close()


def test_migration_does_not_drop_existing_schema(fresh_db):
    """Original columns must still exist after migration."""
    conn = get_connection(fresh_db)
    try:
        cols = _columns(conn)
        for required in (
            "id", "source_filename", "extraction_json", "vendor_name",
            "invoice_number", "invoice_date", "due_date", "po_number",
            "subtotal", "gst", "total_amount", "currency",
            "expense_category", "classification_source",
            "validation_flags", "status", "review_note", "reviewer",
            "created_at", "updated_at", "raw_text",
        ):
            assert required in cols, "missing %s" % required
    finally:
        conn.close()


def test_migrations_constant_contains_step7_entry():
    """Source of truth: code must list the STEP 7 migration."""
    versions = [v for v, _desc, _cols in MIGRATIONS]
    assert 1 in versions, "STEP 7 migration version 1 missing"



def test_migration_adds_document_path_column(fresh_db):
    conn = get_connection(fresh_db)
    try:
        assert INVOICES_DOCUMENT_PATH_COLUMN in _columns(conn)
        assert INVOICES_DOCUMENT_SHA256_COLUMN in _columns(conn)
        assert INVOICES_DOCUMENT_SIZE_COLUMN in _columns(conn)
    finally:
        conn.close()


def test_document_columns_are_nullable(fresh_db):
    conn = get_connection(fresh_db)
    try:
        cur = conn.execute(
            "INSERT INTO invoices (vendor_name, total_amount, status) VALUES (?, ?, ?)",
            ("Acme", 100.0, "pending"),
        )
        new_id = int(cur.lastrowid)
        row = conn.execute(
            "SELECT document_path, document_sha256, document_size_bytes FROM invoices WHERE id = ?",
            (new_id,),
        ).fetchone()
        assert row["document_path"] is None
        assert row["document_sha256"] is None
        assert row["document_size_bytes"] is None
    finally:
        conn.close()


def test_migration_step9_recorded_in_schema_version(fresh_db):
    conn = get_connection(fresh_db)
    try:
        v = get_applied_version(conn)
        assert v >= 2, "STEP 9 must be at version >= 2"
    finally:
        conn.close()


def test_migrations_constant_contains_step9_entry():
    versions = [v for v, _desc, _cols in MIGRATIONS]
    assert 2 in versions, "STEP 9 migration version 2 missing"
