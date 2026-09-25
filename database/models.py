"""SQLite schema for OilOps AI.

Design notes
------------
* invoices table defined up-front for V1.
* status has CHECK constraint.
* ix_invoices_dup_check is NON-UNIQUE; re-uploads accepted; STEP 5 flags.
* created_at/updated_at use CURRENT_TIMESTAMP (ISO-8601).
* Monetary fields stored as REAL."""

from __future__ import annotations

import sqlite3
from typing import Iterable



INVOICES_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_filename TEXT,
    raw_text TEXT,
    extraction_json TEXT,
    vendor_name TEXT,
    invoice_number TEXT,
    invoice_date TEXT,
    due_date TEXT,
    po_number TEXT,
    description TEXT,
    currency TEXT,
    subtotal REAL,
    gst REAL,
    total_amount REAL,
    expense_category TEXT,
    classification_source TEXT,
    validation_flags TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected')),
    review_note TEXT,
    reviewer TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

INVOICES_DUP_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS ix_invoices_dup_check ON invoices (vendor_name, invoice_number, total_amount);
"""

INVOICES_STATUS_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS ix_invoices_status ON invoices (status);
"""

INVOICES_DUE_DATE_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS ix_invoices_due_date ON invoices (due_date);
"""

# Trigger keeps updated_at fresh.
INVOICES_UPDATED_AT_TRIGGER_DDL = """
CREATE TRIGGER IF NOT EXISTS trg_invoices_updated_at
AFTER UPDATE ON invoices
FOR EACH ROW
BEGIN
    UPDATE invoices SET updated_at = CURRENT_TIMESTAMP WHERE id = OLD.id;
END;
"""

ALL_DDL = (INVOICES_TABLE_DDL, INVOICES_DUP_INDEX_DDL, INVOICES_STATUS_INDEX_DDL, INVOICES_DUE_DATE_INDEX_DDL, INVOICES_UPDATED_AT_TRIGGER_DDL)



def apply_schema(conn):
    """Apply all DDL statements. Idempotent."""
    for stmt in ALL_DDL:
        conn.executescript(stmt)
    conn.commit()


def list_tables(conn):
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type = ? ORDER BY name", ("table",))
    return [row[0] for row in cur.fetchall()]


def list_indexes(conn):
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type = ? ORDER BY name", ("index",))
    return [row[0] for row in cur.fetchall()]


def describe(conn, table):
    """Yield column info rows for a table (PRAGMA table_info)."""
    return conn.execute("PRAGMA table_info(" + table + ")")

