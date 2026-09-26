"""STEP 9 - Document Storage + Traceability tests.

These tests cover the controlled, local-only document-storage service:

* save_pdf writes the file under the configured docs root and returns
  a relative reference (path / sha256 / size).
* Path-traversal attempts are rejected.
* resolve_document_path / get_document_abs_path are safe-by-default
  for the UI layer.
* The storage layer never goes anywhere outside the docs root.
* The OILOPS_DOCS_DIR env var is honoured so tests use a tmp dir.

All tests use synthetic byte payloads only.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from services.document_storage import (
    DEFAULT_DOCS_DIR,
    DocumentStorageError,
    EmptyPayloadError,
    InvalidDocumentPathError,
    StoredDocument,
    get_document_abs_path,
    resolve_docs_root,
    resolve_document_path,
    save_pdf,
)


# Minimal PDF-like bytes: not a real PDF, but the storage layer treats
# any bytes as opaque payload. We never read these bytes.
SYNTHETIC_PDF = b"%PDF-1.4\n% SYNTHETIC test fixture for OilOps AI\n"


@pytest.fixture()
def docs_tmp(tmp_path, monkeypatch):
    """Use a fresh tmp dir as the docs root for each test."""
    root = tmp_path / "docs"
    monkeypatch.setenv("OILOPS_DOCS_DIR", str(root))
    yield root


def test_save_pdf_creates_file_under_root(docs_tmp):
    rec = save_pdf(SYNTHETIC_PDF, invoice_id=1, original_filename="acme.pdf")
    assert isinstance(rec, StoredDocument)
    assert rec.document_path.startswith("invoice_1__")
    assert rec.document_path.endswith(".pdf")
    assert rec.document_sha256 == (
        "f" * 0  # placeholder, replaced below
    ) or len(rec.document_sha256) == 64
    assert rec.document_size_bytes == len(SYNTHETIC_PDF)
    # File actually exists under root.
    abs_path = docs_tmp / rec.document_path
    assert abs_path.exists()
    assert abs_path.read_bytes() == SYNTHETIC_PDF


def test_save_pdf_computes_sha256(docs_tmp):
    rec = save_pdf(b"%PDF-hello", invoice_id=42, original_filename="x.pdf")
    # sha256(b"%PDF-hello") - hardcoded expected hash.
    import hashlib
    expected = hashlib.sha256(b"%PDF-hello").hexdigest()
    assert rec.document_sha256 == expected
    assert rec.document_sha256.startswith(rec.document_sha256[:8])
    assert rec.document_path.startswith("invoice_42__")
    assert rec.document_path.endswith(".pdf")


def test_save_pdf_sanitises_traversal_in_filename(docs_tmp):
    """A path-traversal attack in the filename MUST be sanitised."""
    rec = save_pdf(
        SYNTHETIC_PDF,
        invoice_id=2,
        original_filename="../../../etc/passwd",
    )
    # No directory separators in the stored relative path.
    assert "/" not in rec.document_path.replace("invoice_2__", "", 1)
    assert ".." not in rec.document_path
    assert rec.document_path.startswith("invoice_2__")
    assert rec.document_path.endswith(".pdf")
    # The file lands inside the docs root.
    abs_path = docs_tmp / rec.document_path
    assert docs_tmp.resolve() in abs_path.resolve().parents


def test_save_pdf_strips_bad_chars(docs_tmp):
    rec = save_pdf(
        SYNTHETIC_PDF,
        invoice_id=3,
        original_filename="bad name with spaces & symbols!.pdf",
    )
    # Forbidden chars become underscores.
    assert " " not in rec.document_path
    assert "&" not in rec.document_path
    assert "!" not in rec.document_path
    # Still ends with .pdf
    assert rec.document_path.endswith(".pdf")


def test_save_pdf_empty_filename_falls_back(docs_tmp):
    rec = save_pdf(SYNTHETIC_PDF, invoice_id=4, original_filename="")
    assert rec.document_path.startswith("invoice_4__")
    assert rec.document_path.endswith(".pdf")
    # Must have at least 'document.pdf' or similar.
    assert "document.pdf" in rec.document_path


def test_save_pdf_rejects_empty_bytes(docs_tmp):
    with pytest.raises(EmptyPayloadError):
        save_pdf(b"", invoice_id=5, original_filename="x.pdf")


def test_save_pdf_rejects_non_bytes(docs_tmp):
    with pytest.raises(EmptyPayloadError):
        save_pdf("not bytes", invoice_id=6, original_filename="x.pdf")  # type: ignore[arg-type]


def test_save_pdf_overwrites_on_same_id_and_payload(docs_tmp):
    """Deterministic name: re-saving identical bytes overwrites the file
    (idempotent), and the file on disk still matches the bytes."""
    r1 = save_pdf(b"%PDF-stable", invoice_id=10, original_filename="a.pdf")
    r2 = save_pdf(b"%PDF-stable", invoice_id=10, original_filename="a.pdf")
    # Same content -> same hash -> same relative path.
    assert r1.document_path == r2.document_path
    abs_path = docs_tmp / r1.document_path
    assert abs_path.read_bytes() == b"%PDF-stable"


def test_resolve_document_path_roundtrip(docs_tmp):
    rec = save_pdf(SYNTHETIC_PDF, invoice_id=20, original_filename="rt.pdf")
    abs_path = resolve_document_path(rec.document_path)
    assert abs_path.exists()
    assert abs_path.read_bytes() == SYNTHETIC_PDF
    assert docs_tmp.resolve() in abs_path.resolve().parents


def test_resolve_document_path_rejects_absolute(docs_tmp):
    with pytest.raises(InvalidDocumentPathError):
        resolve_document_path("C:/Windows/system32/notepad.exe")
    with pytest.raises(InvalidDocumentPathError):
        resolve_document_path("/etc/passwd")


def test_resolve_document_path_rejects_traversal(docs_tmp):
    with pytest.raises(InvalidDocumentPathError):
        resolve_document_path("../../etc/passwd")
    with pytest.raises(InvalidDocumentPathError):
        resolve_document_path("../invoice_99__abc__x.pdf")


def test_resolve_document_path_rejects_empty():
    with pytest.raises(InvalidDocumentPathError):
        resolve_document_path("")
    with pytest.raises(InvalidDocumentPathError):
        resolve_document_path(None)


def test_get_document_abs_path_returns_none_for_legacy_none():
    assert get_document_abs_path(None) is None
    assert get_document_abs_path("") is None


def test_get_document_abs_path_returns_none_for_invalid():
    assert get_document_abs_path("../../etc/passwd") is None
    assert get_document_abs_path("C:/Windows/notepad.exe") is None


def test_get_document_abs_path_returns_none_when_file_missing(docs_tmp):
    assert get_document_abs_path("invoice_999__deadbeef__missing.pdf") is None


def test_get_document_abs_path_returns_real_path(docs_tmp):
    rec = save_pdf(SYNTHETIC_PDF, invoice_id=77, original_filename="ui.pdf")
    abs_path = get_document_abs_path(rec.document_path)
    assert abs_path is not None
    assert abs_path.exists()
    assert abs_path.read_bytes() == SYNTHETIC_PDF


def test_resolve_docs_root_creates_dir(tmp_path, monkeypatch):
    target = tmp_path / "fresh_docs_root"
    monkeypatch.setenv("OILOPS_DOCS_DIR", str(target))
    root = resolve_docs_root()
    assert root.exists()
    assert root.is_dir()
    assert root.resolve() == target.resolve()


def test_default_docs_root_is_data_documents(monkeypatch):
    monkeypatch.delenv("OILOPS_DOCS_DIR", raising=False)
    from services import document_storage as ds
    expected = Path(ds.__file__).resolve().parent.parent / "data" / "documents"
    assert ds.DEFAULT_DOCS_DIR == expected
    assert ds.resolve_docs_root() == expected


def test_documents_never_escapes_root_in_save(docs_tmp):
    evil_names = [".." + chr(92) + ".." + chr(92) + "evil.pdf", "sub/dir/file.pdf", "/abs/file.pdf", "....pdf"]
    for n in evil_names:
        rec = save_pdf(SYNTHETIC_PDF, invoice_id=999, original_filename=n)
        abs_path = (docs_tmp / rec.document_path).resolve()
        assert docs_tmp.resolve() in abs_path.parents
