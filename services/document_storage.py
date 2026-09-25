"""STEP 9 - Local document storage and traceability for OilOps AI.

DESIGN PRINCIPLES
-----------------
1. The raw PDF bytes are NEVER persisted into SQLite. SQLite only stores
   a *relative* document reference (path, sha256, byte size).
2. Original PDFs MUST live under a single, git-ignored root directory:
   ``<project_root>/data/documents/`` by default. The root can be
   overridden via the ``OILOPS_DOCS_DIR`` environment variable (used
   by tests with a tmp_path).
3. Filenames are deterministic and human-traceable:
       invoice_<invoice_id>__<sha8>__<safe_filename>.pdf
4. The storage layer MUST defend against path-traversal.
5. The original PDF is NEVER sent to the LLM. The AI-safe payload
   path operates on the structured InvoiceExtraction and never sees
   this module.

BACKWARDS COMPATIBILITY
-----------------------
Old invoice rows have ``document_path IS NULL``. All getters return
None for the new fields and no code path errors out on NULL.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DOCS_DIR = PROJECT_ROOT / "data" / "documents"

# Strict filename allowlist: letters, digits, dot, dash, underscore.
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")

# Hard cap on the safe-filename segment to keep storage paths short.
MAX_SAFE_FILENAME_LEN = 80


class DocumentStorageError(Exception):
    """Base error for the document_storage module."""


class InvalidDocumentPathError(DocumentStorageError):
    """Raised when a stored relative path cannot be safely resolved."""


class EmptyPayloadError(DocumentStorageError):
    """Raised when an empty payload is supplied for storage."""


@dataclass
class StoredDocument:
    """A small, immutable record describing a stored PDF."""

    document_path: str        # relative to docs root, forward-slash separated
    document_sha256: str
    document_size_bytes: int


# ---------------------------------------------------------------------------
# Root resolution
# ---------------------------------------------------------------------------

def resolve_docs_root() -> Path:
    """Return the configured document-storage root, creating it if needed.

    Resolution order:
        1. ``OILOPS_DOCS_DIR`` env var (absolute or relative)
        2. ``<project_root>/data/documents`` (the default)
    """
    env = os.getenv("OILOPS_DOCS_DIR")
    if env:
        root = Path(env)
        if not root.is_absolute():
            root = PROJECT_ROOT / root
    else:
        root = DEFAULT_DOCS_DIR
    root.mkdir(parents=True, exist_ok=True)
    return root


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sanitise_filename(name: str) -> str:
    """Return a filesystem-safe rendering of ``name``."""
    base = os.path.basename(name or "")
    safe = _SAFE_FILENAME_RE.sub("_", base).strip("._")
    if not safe:
        safe = "document.pdf"
    if len(safe) > MAX_SAFE_FILENAME_LEN:
        safe = safe[:MAX_SAFE_FILENAME_LEN]
    if not safe.lower().endswith(".pdf"):
        safe = safe + ".pdf"
    return safe


def _compute_sha256(data: bytes) -> str:
    return hashlib.sha256(bytes(data)).hexdigest()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def save_pdf(
    pdf_bytes: bytes,
    *,
    invoice_id: int,
    original_filename: str = "",
) -> StoredDocument:
    """Persist ``pdf_bytes`` under the docs root and return a record.

    Returns a StoredDocument only when the file is actually written to
    disk; callers must check ``document_path`` is non-empty.
    """
    if not isinstance(pdf_bytes, (bytes, bytearray)):
        raise EmptyPayloadError("pdf_bytes must be bytes.")
    if len(pdf_bytes) == 0:
        raise EmptyPayloadError("Cannot store an empty PDF.")

    data = bytes(pdf_bytes)
    sha = _compute_sha256(data)
    safe_name = _sanitise_filename(original_filename)
    sha8 = sha[:8]

    rel = "invoice_{:d}__{}__{}".format(int(invoice_id), sha8, safe_name)
    root = resolve_docs_root()
    abs_path = root / rel

    # Path-traversal defence: abs_path must be inside root.
    try:
        abs_path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise InvalidDocumentPathError(
            "Resolved path escapes docs root: %s" % rel
        ) from exc

    # Write atomically: tmp + rename, so a crash never leaves a
    # half-written PDF on disk.
    tmp_path = abs_path.with_suffix(abs_path.suffix + ".tmp")
    with open(tmp_path, "wb") as f:
        f.write(data)
    os.replace(tmp_path, abs_path)

    return StoredDocument(
        document_path=rel.replace(os.sep, "/"),
        document_sha256=sha,
        document_size_bytes=len(data),
    )


def resolve_document_path(rel_path: str) -> Path:
    """Resolve a stored relative path to an absolute filesystem path.

    Defends against path traversal: any resolved path that escapes the
    configured docs root raises InvalidDocumentPathError.
    """
    if not rel_path or not isinstance(rel_path, str):
        raise InvalidDocumentPathError("Empty document path.")
    root = resolve_docs_root()
    # Normalise: reject absolute paths up front.
    if os.path.isabs(rel_path) or rel_path.startswith(("/", "\\")):
        raise InvalidDocumentPathError(
            "Document path must be relative, got: %r" % rel_path
        )
    candidate = (root / rel_path).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise InvalidDocumentPathError(
            "Document path escapes docs root: %r" % rel_path
        ) from exc
    return candidate


def get_document_abs_path(rel_path: Optional[str]) -> Optional[Path]:
    """Convenience wrapper used by the UI layer.

    Returns None when ``rel_path`` is falsy (legacy row), the absolute
    path on success, and None (NOT an exception) if the file is
    missing on disk - the UI must tolerate gaps without crashing.
    """
    if not rel_path:
        return None
    try:
        abs_path = resolve_document_path(rel_path)
    except InvalidDocumentPathError:
        return None
    if not abs_path.exists():
        return None
    return abs_path
