"""Tests for SQLite layer (STEP 1 + schema fix)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from database.db import get_connection, init_db, transaction
from database.models import ALL_DDL, describe, list_indexes, list_tables


@pytest.fixture()
def tmp_db(tmp_path):
    return tmp_path / "test_oilops.db"



def test_init_db_creates_invoices_table(tmp_db):
    init_db(str(tmp_db))
    conn = get_connection(str(tmp_db))
    try:
        assert "invoices" in list_tables(conn)
    finally:
        conn.close()


def test_init_db_is_idempotent(tmp_db):
    init_db(str(tmp_db))
    init_db(str(tmp_db))
    conn = get_connection(str(tmp_db))
    try:
        assert list_tables(conn).count("invoices") == 1
    finally:
        conn.close()



def test_schema_contains_expected_columns(tmp_db):
    init_db(str(tmp_db))
    conn = get_connection(str(tmp_db))
    try:
        cols = {row["name"] for row in describe(conn, "invoices")}
    finally:
        conn.close()
    required = {"id", "vendor_name", "invoice_number", "invoice_date", "due_date", "po_number", "subtotal", "gst", "total_amount", "currency", "status", "created_at", "updated_at"}
    missing = required - cols
    assert not missing, f"Missing: {missing}"



def test_status_check_constraint_accepts_valid_values(tmp_db):
    init_db(str(tmp_db))
    with transaction(str(tmp_db)) as conn:
        conn.execute("INSERT INTO invoices (status, vendor_name, total_amount) VALUES (?, ?, ?)", ("pending", "Rocky Mountain Drilling Services", 1234.56))
    conn = get_connection(str(tmp_db))
    try:
        row = conn.execute("SELECT status FROM invoices WHERE vendor_name = ?", ("Rocky Mountain Drilling Services",)).fetchone()
        assert row is not None and row["status"] == "pending"
    finally:
        conn.close()


def test_status_check_constraint_rejects_invalid_values(tmp_db):
    init_db(str(tmp_db))
    with pytest.raises(sqlite3.IntegrityError):
        with transaction(str(tmp_db)) as conn:
            conn.execute("INSERT INTO invoices (status, vendor_name) VALUES (?, ?)", ("paid", "X"))



def test_dup_index_is_created(tmp_db):
    """dup index must exist by NEW name"""
    init_db(str(tmp_db))
    conn = get_connection(str(tmp_db))
    try:
        idx = list_indexes(conn)
        assert "ix_invoices_dup_check" in idx
        assert "ux_invoices_dup_check" not in idx
    finally:
        conn.close()


def test_dup_index_is_not_unique(tmp_db):
    """A duplicate upload must be accepted (human-review workflow)."""
    init_db(str(tmp_db))
    with transaction(str(tmp_db)) as conn:
        conn.execute("INSERT INTO invoices (vendor_name, invoice_number, total_amount) VALUES (?, ?, ?)", ("Rocky Mountain Drilling Services", "RMD-2026-001", 10500.00))
        conn.execute("INSERT INTO invoices (vendor_name, invoice_number, total_amount) VALUES (?, ?, ?)", ("Rocky Mountain Drilling Services", "RMD-2026-001", 10500.00))
    conn = get_connection(str(tmp_db))
    try:
        rows = conn.execute("SELECT id FROM invoices WHERE vendor_name=? AND invoice_number=? AND total_amount=?", ("Rocky Mountain Drilling Services", "RMD-2026-001", 10500.00)).fetchall()
    finally:
        conn.close()
    assert len(rows) == 2, "DB must allow duplicate row to be inserted for STEP 5 flag."



def test_updated_at_trigger_refreshes_on_update(tmp_db):
    init_db(str(tmp_db))
    with transaction(str(tmp_db)) as conn:
        conn.execute("INSERT INTO invoices (vendor_name, status) VALUES (?, ?)", ("Northern Well Services Inc.", "pending"))
    conn = get_connection(str(tmp_db))
    try:
        original = conn.execute("SELECT created_at, updated_at FROM invoices WHERE vendor_name = ?", ("Northern Well Services Inc.",)).fetchone()
        with transaction(str(tmp_db)) as c2:
            c2.execute("UPDATE invoices SET status = ? WHERE vendor_name = ?", ("approved", "Northern Well Services Inc."))
    finally:
        conn.close()
    conn = get_connection(str(tmp_db))
    try:
        after = conn.execute("SELECT created_at, updated_at FROM invoices WHERE vendor_name = ?", ("Northern Well Services Inc.",)).fetchone()
    finally:
        conn.close()
    assert original is not None and after is not None
    assert after["created_at"] == original["created_at"]
    assert after["updated_at"] >= original["updated_at"]



def test_transaction_rolls_back_on_exception(tmp_db):
    init_db(str(tmp_db))
    class _Boom(Exception):
        pass
    with pytest.raises(_Boom):
        with transaction(str(tmp_db)) as conn:
            conn.execute("INSERT INTO invoices (vendor_name) VALUES (?)", ("Prairie Field Equipment Ltd.",))
            raise _Boom()
    conn = get_connection(str(tmp_db))
    try:
        n = conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
    finally:
        conn.close()
    assert n == 0, "Transaction must roll back on exception."


def test_all_ddl_is_iterable(tmp_db):
    assert len(ALL_DDL) >= 1
    for stmt in ALL_DDL:
        assert isinstance(stmt, str) and stmt.strip()

