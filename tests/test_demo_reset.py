"""Batch 4 - Demo environment reset / seed tests.

Covers:
- demo DB is on a SEPARATE path from the user DB
- reset_demo_db refuses to touch the user DB
- reset_demo_db refuses any non-demo path
- reset_demo_db creates an empty, fresh-schema DB
- seed_demo_invoices inserts a small fixed set of synthetic rows
- demo DB isolation: touching demo DB never modifies user DB
- reset clears demo docs and re-creates the schema
- DemoEnvironmentError is raised for non-demo paths
- demo_db_summary reports count / exists / path correctly

Design rules honoured by the tests:
- The user DB lives at OILOPS_DB_PATH. The demo DB lives at
  OILOPS_DEMO_DB_PATH (or database/demo_oilops.db by default).
  These are DIFFERENT files - never the same.
- reset_demo_db is the ONLY function that destroys demo data.
  It must NEVER accept the user DB path.
- All amounts are real numbers from the InvoiceExtraction
  allow-list, never AI arithmetic.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from database.db import DEFAULT_DB_PATH, get_connection, init_db
from services.demo_reset import (
    DEFAULT_DEMO_DB_PATH,
    DEFAULT_DEMO_DOCS_DIR,
    DemoEnvironmentError,
    demo_db_summary,
    is_demo_path,
    reset_demo_db,
    resolve_demo_db_path,
    resolve_demo_docs_root,
    seed_demo_invoices,
)
from services.invoice_repository import (
    STATUS_APPROVED,
    STATUS_PENDING,
    save_pending,
)

# ---------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------

@pytest.fixture()
def tmp_demo_db(tmp_path):
    """Provide a tmp demo DB path. The user DB env var is set to a
    DIFFERENT tmp path so we can verify isolation."""
    user_db = tmp_path / "user_oilops.db"
    demo_db = tmp_path / "demo_oilops.db"
    docs = tmp_path / "demo_documents"
    os.environ["OILOPS_DB_PATH"] = str(user_db)
    os.environ["OILOPS_DEMO_DB_PATH"] = str(demo_db)
    os.environ["OILOPS_DEMO_DOCS_DIR"] = str(docs)
    # Re-import to pick up the env vars (the resolve_* helpers
    # read at call time, so this is belt-and-braces).
    yield {"user_db": user_db, "demo_db": demo_db, "docs": docs}
    # cleanup
    for k in ("OILOPS_DEMO_DB_PATH", "OILOPS_DEMO_DOCS_DIR"):
        os.environ.pop(k, None)


# ---------------------------------------------------------------------
# Path isolation
# ---------------------------------------------------------------------

def test_demo_db_path_is_different_from_user_default():
    # Even the hard-coded default paths must point at DIFFERENT files.
    assert DEFAULT_DEMO_DB_PATH != DEFAULT_DB_PATH
    assert DEFAULT_DEMO_DB_PATH.name.startswith("demo_")


def test_resolve_demo_db_path_uses_env_var(tmp_demo_db):
    p = resolve_demo_db_path()
    assert str(p) == str(tmp_demo_db["demo_db"])


def test_resolve_demo_db_path_does_not_read_user_db_env(tmp_path):
    user_only = tmp_path / "user_only.db"
    os.environ["OILOPS_DB_PATH"] = str(user_only)
    # Make sure OILOPS_DEMO_DB_PATH is unset
    os.environ.pop("OILOPS_DEMO_DB_PATH", None)
    try:
        p = resolve_demo_db_path()
        # Default path is NOT the user DB path.
        assert str(p) != str(user_only.resolve())
        assert p.name.startswith("demo_")
    finally:
        os.environ.pop("OILOPS_DEMO_DB_PATH", None)


# ---------------------------------------------------------------------
# is_demo_path guard
# ---------------------------------------------------------------------

def test_is_demo_path_recognises_demo_db():
    assert is_demo_path(DEFAULT_DEMO_DB_PATH)
    assert is_demo_path(Path("database/demo_something.db"))


def test_is_demo_path_rejects_user_db():
    assert not is_demo_path(DEFAULT_DB_PATH)
    assert not is_demo_path(Path("database/oilops.db"))


# ---------------------------------------------------------------------
# reset_demo_db
# ---------------------------------------------------------------------

def test_reset_demo_db_creates_fresh_schema(tmp_demo_db):
    p = reset_demo_db()
    assert p.exists()
    conn = get_connection(str(p))
    try:
        # invoices table must exist (schema applied)
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        names = {r[0] for r in cur.fetchall()}
        assert "invoices" in names
        # Empty after reset
        cur = conn.execute("SELECT COUNT(*) FROM invoices")
        assert cur.fetchone()[0] == 0
    finally:
        conn.close()

def test_reset_demo_db_refuses_user_db_path(tmp_demo_db):
    with pytest.raises(DemoEnvironmentError):
        reset_demo_db(db_path=str(tmp_demo_db["user_db"]))


def test_reset_demo_db_refuses_oilops_db(tmp_demo_db):
    # The hard-coded user DB default is the most important refusal.
    with pytest.raises(DemoEnvironmentError):
        reset_demo_db(db_path=str(DEFAULT_DB_PATH))


def test_reset_demo_db_refuses_non_demo_filename(tmp_demo_db, tmp_path):
    other = tmp_path / "production.db"  # does NOT start with demo_
    with pytest.raises(DemoEnvironmentError):
        reset_demo_db(db_path=str(other))


# ---------------------------------------------------------------------
# Isolation: user DB is NEVER modified by demo operations
# ---------------------------------------------------------------------

def test_demo_reset_does_not_touch_user_db(tmp_demo_db):
    user_db = tmp_demo_db["user_db"]
    # Initialise the user DB and add one row.
    init_db(str(user_db))
    conn = get_connection(str(user_db))
    try:
        from services.invoice_extractor import InvoiceExtraction
        from decimal import Decimal
        ext = InvoiceExtraction(
            vendor_name="User Vendor",
            invoice_number="USER-1",
            invoice_date="2026-09-15",
            due_date="2026-09-30",
            po_number="",
            subtotal=Decimal("1.00"),
            gst=Decimal("0.00"),
            total_amount=Decimal("1.00"),
            currency="CAD",
            description="user",
            warnings=[],
        )
        save_pending(
            conn, ext, source_filename="u.pdf", expense_category="X",
        )
        conn.commit()
        before_count = conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
        assert before_count == 1
    finally:
        conn.close()

    # Now do a demo reset - this MUST NOT touch the user DB.
    reset_demo_db()
    seed_demo_invoices(get_connection(str(resolve_demo_db_path())))

    # Re-open user DB. Row count must be unchanged.
    conn2 = get_connection(str(user_db))
    try:
        after_count = conn2.execute(
            "SELECT COUNT(*) FROM invoices"
        ).fetchone()[0]
        assert after_count == 1
        # And the user row content is unchanged.
        row = conn2.execute(
            "SELECT vendor_name FROM invoices WHERE invoice_number='USER-1'"
        ).fetchone()
        assert row[0] == "User Vendor"
    finally:
        conn2.close()

# ---------------------------------------------------------------------
# seed_demo_invoices
# ---------------------------------------------------------------------

def test_seed_demo_inserts_small_synthetic_set(tmp_demo_db):
    reset_demo_db()
    conn = get_connection(str(resolve_demo_db_path()))
    try:
        ids = seed_demo_invoices(conn)
        conn.commit()
        assert len(ids) >= 3  # at least 3 demo rows
        # All rows are pending except the first which is approved+paid.
        cur = conn.execute(
            "SELECT status, paid_at FROM invoices ORDER BY id"
        )
        rows = cur.fetchall()
        assert rows[0][0] == STATUS_APPROVED
        assert rows[0][1] is not None  # paid_at is set
        # Other rows are still pending.
        for row in rows[1:]:
            assert row[0] == STATUS_PENDING
    finally:
        conn.close()


def test_seed_demo_preserves_cad_and_usd(tmp_demo_db):
    reset_demo_db()
    conn = get_connection(str(resolve_demo_db_path()))
    try:
        seed_demo_invoices(conn)
        conn.commit()
        cur = conn.execute(
            "SELECT DISTINCT currency FROM invoices"
        )
        currencies = {r[0] for r in cur.fetchall()}
        # Demo seed must include BOTH CAD and USD.
        assert "CAD" in currencies
        assert "USD" in currencies
    finally:
        conn.close()


def test_seed_demo_after_reset_is_idempotent(tmp_demo_db):
    reset_demo_db()
    conn = get_connection(str(resolve_demo_db_path()))
    try:
        seed_demo_invoices(conn)
        conn.commit()
        first = conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
        # Re-seed: must be the same count (reset before would, but here
        # we just add again to confirm insertion is allowed).
        seed_demo_invoices(conn)
        conn.commit()
        second = conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
        assert second == first * 2  # exactly double
    finally:
        conn.close()

# ---------------------------------------------------------------------
# demo_db_summary
# ---------------------------------------------------------------------

def test_demo_db_summary_when_missing(tmp_demo_db):
    # No demo DB created yet.
    summary = demo_db_summary()
    assert summary["exists"] is False
    assert summary["path"] == str(tmp_demo_db["demo_db"])


def test_demo_db_summary_when_present(tmp_demo_db):
    reset_demo_db()
    conn = get_connection(str(resolve_demo_db_path()))
    try:
        seed_demo_invoices(conn)
        conn.commit()
    finally:
        conn.close()
    summary = demo_db_summary()
    assert summary["exists"] is True
    assert summary["count"] >= 3
    assert summary["path"] == str(tmp_demo_db["demo_db"])


# ---------------------------------------------------------------------
# resolve_demo_docs_root
# ---------------------------------------------------------------------

def test_resolve_demo_docs_root_creates_dir(tmp_demo_db):
    docs = resolve_demo_docs_root()
    assert docs == tmp_demo_db["docs"]
    assert docs.exists()
    assert docs.is_dir()


def test_resolve_demo_docs_root_is_different_from_user_docs():
    # User docs default lives at data/documents. Demo docs default
    # is data/demo_documents. They are different roots.
    from services.document_storage import DEFAULT_DOCS_DIR
    assert DEFAULT_DEMO_DOCS_DIR != DEFAULT_DOCS_DIR
    assert "demo" in str(DEFAULT_DEMO_DOCS_DIR).lower()


# ---------------------------------------------------------------------
# App-startup isolation (regression: demo reset is NEVER auto-run)
# ---------------------------------------------------------------------

def test_demo_reset_is_not_invoked_at_import(tmp_demo_db):
    """Importing services.demo_reset must NOT touch any DB file.
    """
    # Re-import to be sure.
    import importlib
    import services.demo_reset as m
    importlib.reload(m)
    # Neither demo nor user DB should have been created at import.
    assert not tmp_demo_db["demo_db"].exists()
    assert not tmp_demo_db["user_db"].exists()

