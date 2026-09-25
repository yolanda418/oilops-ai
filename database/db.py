"""SQLite connection management for OilOps AI.

Responsibilities
----------------
* Resolve the database path from env (`OILOPS_DB_PATH`) or fall back to
  `database/oilops.db` relative to the project root.
* Open connections in a safe default style:
    - `row_factory = sqlite3.Row` so services can read columns by name.
    - `PRAGMA foreign_keys = ON` (defensive; we don't use FKs yet but
      this prevents silent breakage if a future migration adds them).
    - `PRAGMA journal_mode = WAL` for safer concurrent reads while
      the Streamlit app is running.
* Expose `init_db()` which is idempotent and safe to call on every
  app start. Both the schema bootstrap AND all migrations are applied.
* Provide a context manager (`transaction`) for write operations so
  services never forget to commit/rollback.

The DB file is local. There is no network exposure.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from dotenv import load_dotenv

# Load .env exactly once when this module is first imported.
# Subsequent imports are no-ops.
load_dotenv()


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "database" / "oilops.db"


def resolve_db_path(path: Optional[str] = None) -> Path:
    """Resolve the DB file path.

    Order of precedence:
        1. explicit `path` argument (used by tests)
        2. `OILOPS_DB_PATH` env var
        3. `database/oilops.db` relative to project root
    """
    if path:
        p = Path(path)
    else:
        env = os.getenv("OILOPS_DB_PATH")
        p = Path(env) if env else DEFAULT_DB_PATH

    if not p.is_absolute():
        p = PROJECT_ROOT / p

    p.parent.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------------------
# Connection factory
# ---------------------------------------------------------------------------

def get_connection(path: Optional[str] = None) -> sqlite3.Connection:
    """Open and return a new SQLite connection.

    Caller is responsible for closing it. For most app code, prefer
    the `transaction()` context manager below.
    """
    db_path = resolve_db_path(path)
    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    # Defensive pragmas
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def transaction(path: Optional[str] = None) -> Iterator[sqlite3.Connection]:
    """Context manager wrapping a write transaction.

    Commits on clean exit, rolls back on exception, always closes.
    """
    conn = get_connection(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

def init_db(path: Optional[str] = None) -> Path:
    """Create schema if missing. Apply migrations. Returns the resolved DB path.

    Both ``apply_schema`` and ``apply_migrations`` are idempotent.
    Calling ``init_db`` on a fully up-to-date database is a no-op
    that returns the same path.
    """
    # Import locally to avoid a circular import at module load time.
    from .models import apply_schema
    from .migrations import apply_migrations

    db_path = resolve_db_path(path)
    conn = get_connection(str(db_path))
    try:
        apply_schema(conn)
        # STEP 7: payment-tracking columns. Safe no-op on already-migrated DBs.
        apply_migrations(conn)
        conn.commit()
    finally:
        conn.close()
    return db_path


if __name__ == "__main__":  # pragma: no cover
    # Allow `python -m database.db` to bootstrap a local DB.
    p = init_db()
    print(f"Initialized DB at: {p}")

