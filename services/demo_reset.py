"""Batch 4 - Demo Environment Reset / Seed Support.

This module is the EXPLICIT, isolated plumbing for running OilOps AI
on a clean, synthetic-data demo database. It is NOT auto-invoked by
the app. The user DB at OILOPS_DB_PATH is NEVER touched.

Resolution order:
    1. explicit ``db_path`` argument (used by tests)
    2. ``OILOPS_DEMO_DB_PATH`` env var
    3. ``<project_root>/database/demo_oilops.db`` (the default)

By default the demo DB lives at ``database/demo_oilops.db``, which
is a DIFFERENT SQLite file from the user DB. Demo documents live
under ``<project_root>/data/demo_documents/`` by default, again
a separate root. The user DB and docs root are never modified.

The demo seed (`seed_demo_invoices`) inserts a small fixed set of
synthetic invoices - this batch does NOT generate 12-15 realistic
PDFs. That is the next milestone (Synthetic Business E2E Test).
"""
from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path
from typing import Optional, Tuple

from database.db import init_db as _init_user_db
from .invoice_extractor import InvoiceExtraction
from .invoice_repository import (
    STATUS_APPROVED,
    STATUS_PENDING,
    approve_invoice,
    mark_paid,
    save_pending,
)


# ---------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------

DEMO_PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DEMO_DB_PATH = DEMO_PROJECT_ROOT / "database" / "demo_oilops.db"
DEFAULT_DEMO_DOCS_DIR = DEMO_PROJECT_ROOT / "data" / "demo_documents"


def resolve_demo_db_path(db_path: Optional[str] = None) -> Path:
    """Resolve the demo DB path. Never reads the user DB env var.
    """
    if db_path:
        p = Path(db_path)
    else:
        env = os.getenv("OILOPS_DEMO_DB_PATH")
        p = Path(env) if env else DEFAULT_DEMO_DB_PATH
    if not p.is_absolute():
        p = DEMO_PROJECT_ROOT / p
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def resolve_demo_docs_root(docs_root: Optional[str] = None) -> Path:
    """Resolve the demo documents root. Never reads user docs root.
    """
    if docs_root:
        p = Path(docs_root)
    else:
        env = os.getenv("OILOPS_DEMO_DOCS_DIR")
        p = Path(env) if env else DEFAULT_DEMO_DOCS_DIR
    if not p.is_absolute():
        p = DEMO_PROJECT_ROOT / p
    p.mkdir(parents=True, exist_ok=True)
    return p

# ---------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------

class DemoResetError(Exception):
    """Base error for the demo_reset module."""


class DemoEnvironmentError(DemoResetError):
    """Raised when an attempted demo reset would touch the user DB."""


# ---------------------------------------------------------------------
# Reset / seed
# ---------------------------------------------------------------------

def is_demo_path(db_path: Path) -> bool:
    """Return True when ``db_path`` is clearly a demo DB path.

    A demo DB path either lives under ``database/demo_*.db`` or
    matches the resolved DEMO_DB_PATH exactly. Anything that looks
    like the user DB is refused by ``reset_demo_db``.
    """
    p = Path(db_path).resolve()
    demo = DEFAULT_DEMO_DB_PATH.resolve()
    if p == demo:
        return True
    name = p.name.lower()
    if name.startswith("demo_") and name.endswith(".db"):
        return True
    # Anything inside a dedicated demo dir
    if "demo" in p.parts:
        return True
    return False


def reset_demo_db(db_path=None, docs_root=None) -> Path:
    """Delete the demo DB file (and clear demo docs) and re-init schema.

    Refuses to run when ``db_path`` points at the user DB. The user
    DB lives at OILOPS_DB_PATH (or ``database/oilops.db``) - it is
    NEVER touched.

    Returns the resolved demo DB path after a fresh schema is applied.
    """
    demo_path = resolve_demo_db_path(db_path)
    if not is_demo_path(demo_path):
        raise DemoEnvironmentError(
            "Refusing to reset non-demo DB path: %s" % demo_path
        )
    # Hard guard: if the resolved path equals the user default, refuse.
    user_default = (_init_user_db.__globals__["DEFAULT_DB_PATH"])
    if demo_path.resolve() == Path(user_default).resolve():
        raise DemoEnvironmentError(
            "Refusing to reset - resolved path equals user DB."
        )

    docs_dir = resolve_demo_docs_root(docs_root)

    # 1. remove the DB file (and WAL/SHM if present)
    for suffix in ("", "-wal", "-shm", "-journal"):
        target = demo_path.with_name(demo_path.name + suffix)
        if suffix == "":
            target = demo_path
        try:
            if target.exists():
                target.unlink()
        except OSError:
            pass

    # 2. clear demo docs root (only files we wrote, never the dir)
    if docs_dir.exists():
        for entry in docs_dir.iterdir():
            try:
                if entry.is_file():
                    entry.unlink()
                elif entry.is_dir():
                    shutil.rmtree(entry)
            except OSError:
                pass

    # 3. re-init the schema on a fresh DB
    _init_user_db(str(demo_path))
    return demo_path

# ---------------------------------------------------------------------
# Demo seed (small, deterministic, synthetic only)
# ---------------------------------------------------------------------

def _demo_extraction(vendor, inv_no, inv_date, due_date, po, subtotal,
                     gst, total, currency, description, category):
    return InvoiceExtraction(
        vendor_name=vendor,
        invoice_number=inv_no,
        invoice_date=inv_date,
        due_date=due_date,
        po_number=po,
        description=description,
        subtotal=subtotal,
        gst=gst,
        total_amount=total,
        currency=currency,
        warnings=[],
    )


def seed_demo_invoices(conn, *, expense_category="Equipment"):
    """Insert a small fixed set of synthetic invoices into ``conn``.

    This is the Batch 4 seed: 3 invoices, mixed CAD/USD, mixed
    review status. The Synthetic Business E2E Test will replace
    this with 12-15 realistic PDFs - this is the SAFE scaffolding
    for that future test, nothing more.

    Returns the list of inserted invoice ids.
    """
    rows = [
        _demo_extraction(
            vendor="Prairie Pump Rentals",
            inv_no="PPR-2026-100",
            inv_date="2026-09-15",
            due_date="2026-09-30",
            po="PO-1042",
            subtotal="5000.00",
            gst="250.00",
            total="5250.00",
            currency="CAD",
            description="Equipment rental for wellsite pump.",
            category="Equipment",
        ),
        _demo_extraction(
            vendor="Foothills Lab Services",
            inv_no="FL-2026-077",
            inv_date="2026-09-18",
            due_date="2026-10-02",
            po="PO-1051",
            subtotal="1800.00",
            gst="90.00",
            total="1890.00",
            currency="CAD",
            description="Core analysis - well 14-22.",
            category="Lab Services",
        ),
        _demo_extraction(
            vendor="Northern Transport Inc",
            inv_no="NTI-9981",
            inv_date="2026-09-20",
            due_date="2026-10-05",
            po="",
            subtotal="3200.00",
            gst="0.00",
            total="3200.00",
            currency="USD",
            description="Water hauling - battery 03.",
            category="Transport",
        ),
    ]
    ids = []
    cat = expense_category
    for ext in rows:
        new_id = save_pending(
            conn, ext,
            source_filename="demo_seed.pdf",
            expense_category=cat,
        )
        ids.append(int(new_id))
    # Approve + mark paid the first one to seed a non-pending row.
    if ids:
        approve_invoice(conn, ids[0], reviewer="Demo Seed")
        mark_paid(
            conn, ids[0],
            paid_at="2026-09-25T10:00:00",
            payment_note="seed-paid",
        )
    return ids


def demo_db_summary(db_path=None) -> dict:
    """Return a tiny summary of the demo DB for UI display."""
    demo_path = resolve_demo_db_path(db_path)
    if not demo_path.exists():
        return {"exists": False, "path": str(demo_path)}
    conn = sqlite3.connect(str(demo_path))
    try:
        cur = conn.execute("SELECT COUNT(*) FROM invoices")
        count = int(cur.fetchone()[0])
        return {"exists": True, "path": str(demo_path), "count": count}
    finally:
        conn.close()


__all__ = [
    "DEFAULT_DEMO_DB_PATH",
    "DEFAULT_DEMO_DOCS_DIR",
    "DemoEnvironmentError",
    "DemoResetError",
    "demo_db_summary",
    "is_demo_path",
    "reset_demo_db",
    "resolve_demo_db_path",
    "resolve_demo_docs_root",
    "seed_demo_invoices",
]
