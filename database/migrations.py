"""Idempotent schema migrations for OilOps AI.

STEP 7 adds two columns to the ``invoices`` table to support
Payment Tracking:

    * ``paid_at``      TEXT NULL   -- ISO-8601 timestamp
    * ``payment_note`` TEXT NULL   -- free text, no payment API fields

STEP 9 (Batch 1 - Document Storage + Traceability) adds three columns
to record a reference to the locally-stored original PDF. SQLite
NEVER stores the PDF blob itself; it only stores a *relative* path
plus integrity metadata:

    * ``document_path``       TEXT NULL     -- relative path under docs root
    * ``document_sha256``     TEXT NULL     -- sha256 hex of the PDF bytes
    * ``document_size_bytes`` INTEGER NULL  -- byte size of the PDF

These columns are deliberately additive. The migrations must:

    * never rebuild or drop the existing schema
    * be safe to run multiple times in a row (idempotent)
    * never error out when the column already exists
    * never write to the production local DB during tests

The pattern is:

    PRAGMA table_info(invoices) -> check column names
    ALTER TABLE invoices ADD COLUMN ...    (only if missing)

A small ``schema_version`` table records which migrations have been
applied, so we can add future migrations without re-running the
older ones.
"""
from __future__ import annotations

import sqlite3
from typing import Iterable, List, Optional, Tuple


# STEP 7 - payment tracking columns.
INVOICES_PAID_AT_COLUMN = "paid_at"
INVOICES_PAYMENT_NOTE_COLUMN = "payment_note"

# Ordered list of (version, description, columns_to_add).
# New migrations append to this list. They are applied in order.
# Column names for STEP 9 - document traceability.
INVOICES_DOCUMENT_PATH_COLUMN = "document_path"
INVOICES_DOCUMENT_SHA256_COLUMN = "document_sha256"
INVOICES_DOCUMENT_SIZE_COLUMN = "document_size_bytes"

MIGRATIONS: Tuple[Tuple[int, str, Tuple[str, ...]], ...] = (
    (
        1,
        "STEP 7 - payment tracking columns",
        (INVOICES_PAID_AT_COLUMN, INVOICES_PAYMENT_NOTE_COLUMN),
    ),
    (
        2,
        "STEP 9 - document traceability columns",
        (
            INVOICES_DOCUMENT_PATH_COLUMN,
            INVOICES_DOCUMENT_SHA256_COLUMN,
            INVOICES_DOCUMENT_SIZE_COLUMN,
        ),
    ),
)


def _table_has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Return True iff ``column`` exists on ``table``.

    Uses ``PRAGMA table_info`` so it works against the live schema
    without parsing CREATE statements.
    """
    cur = conn.execute(
        "SELECT 1 FROM pragma_table_info(?) WHERE name = ? LIMIT 1",
        (table, column),
    )
    return cur.fetchone() is not None


def _ensure_schema_version_table(conn: sqlite3.Connection) -> None:
    """Create the tiny ``schema_version`` table if missing.

    Records the highest applied migration version. We only track the
    version number; the actual DDL is encoded in this module so the
    source of truth is the code, not a string column.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            description TEXT
        )
        """
    )


def _applied_version(conn: sqlite3.Connection) -> int:
    """Return the highest migration version already applied (0 if none)."""
    try:
        cur = conn.execute("SELECT MAX(version) FROM schema_version")
    except sqlite3.OperationalError:
        return 0
    row = cur.fetchone()
    if not row or row[0] is None:
        return 0
    return int(row[0])


def _add_column_if_missing(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    column_ddl: str,
) -> bool:
    """Add a single column if it does not already exist.

    Returns True if a column was added, False if it was already there.
    Never raises on the "already exists" path.
    """
    if _table_has_column(conn, table, column):
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column_ddl}")
    return True


def apply_migrations(conn: sqlite3.Connection, *, table: str = "invoices") -> List[str]:
    """Apply every pending migration idempotently.

    Returns a list of human-readable descriptions of the migrations
    that were actually applied on this call. Re-running on a DB that
    is already up-to-date returns an empty list.
    """
    applied: List[str] = []
    _ensure_schema_version_table(conn)
    already = _applied_version(conn)

    for version, description, columns in MIGRATIONS:
        if version <= already:
            continue
        # Per-column DDL is dispatched on the column name. Adding a new
        # column means extending this dispatch; default is nullable TEXT.
        for column in columns:
            if column in (
                INVOICES_PAID_AT_COLUMN,
                INVOICES_PAYMENT_NOTE_COLUMN,
                INVOICES_DOCUMENT_PATH_COLUMN,
                INVOICES_DOCUMENT_SHA256_COLUMN,
            ):
                _add_column_if_missing(
                    conn, table, column, f"{column} TEXT"
                )
            elif column == INVOICES_DOCUMENT_SIZE_COLUMN:
                _add_column_if_missing(
                    conn, table, column, f"{column} INTEGER"
                )
            else:
                # Future-proofing: unknown column, ignore safely.
                _add_column_if_missing(conn, table, column, f"{column} TEXT")
        conn.execute(
            "INSERT OR IGNORE INTO schema_version (version, description) VALUES (?, ?)",
            (version, description),
        )
        applied.append(description)

    return applied


def get_applied_version(conn: sqlite3.Connection) -> int:
    """Public helper for tests."""
    return _applied_version(conn)

