"""
Batch 2 - Batch Invoice Intake tests.

Covers BATCH-LEVEL orchestration of the per-file pipeline:
    1. normal: clean invoice -> Ready
    2. missing PO -> Needs Review
    3. duplicate -> Possible Duplicate (still saved)
    4. amount mismatch -> Needs Review
    5. parsing failure -> Failed (failure isolation)

Plus invariants: document storage, privacy, no auto-approve.
"""
from __future__ import annotations

import os
import tempfile
from datetime import date
from pathlib import Path

import pytest

from database.db import get_connection, init_db
from services.batch_intake import (
    BATCH_MAX_PDF_BYTES,
    BatchIntakeFile,
    process_pdf_batch,
    queue_row_from_item,
)
from services.queue_status import (
    QUEUE_STATUS_FAILED,
    QUEUE_STATUS_NEEDS_REVIEW,
    QUEUE_STATUS_POSSIBLE_DUPLICATE,
    QUEUE_STATUS_READY,
)
from tests.fixtures.make_e2e_invoices import (
    CASE_1_LINES,
    CASE_3_LINES,
    CASE_4_LINES,
    CASE_5_LINES,
    CASE_8_LINES,
    write_pdf,
)

REFERENCE_DATE = date(2026, 9, 22)


def _init_tmp_db():
    tmpdir = tempfile.mkdtemp(prefix="oilops_batch2_")
    db_path = os.path.join(tmpdir, "batch2.db")
    init_db(db_path)
    return get_connection(db_path), tmpdir


def _wrap(lines, tmp_path, name):
    path = Path(tmp_path) / name
    write_pdf(path, lines)
    return path


# 1. NORMAL - clean invoice -> Ready
def test_batch_processes_clean_invoice_as_ready(tmp_path, monkeypatch):
    monkeypatch.setenv("OILOPS_DOCS_DIR", str(tmp_path / "docs"))
    conn, _ = _init_tmp_db()
    pdf_path = _wrap(CASE_1_LINES, tmp_path, "case1_clean.pdf")
    files = [BatchIntakeFile(filename=pdf_path.name, raw_bytes=pdf_path.read_bytes())]
    items = process_pdf_batch(files, conn=conn)
    conn.commit()
    conn.close()
    assert len(items) == 1
    item = items[0]
    assert item.queue_status == QUEUE_STATUS_READY
    assert item.parse_status == "ok"
    assert item.invoice_id is not None
    assert item.document is not None
    assert item.document.document_path.startswith("invoice_")
    abs_path = tmp_path / "docs" / item.document.document_path
    assert abs_path.exists()
    assert abs_path.read_bytes() == pdf_path.read_bytes()
    row = queue_row_from_item(item)
    for key in (
        "filename", "vendor", "invoice_number", "total", "currency",
        "classification", "validation", "status", "invoice_id",
        "document_path", "is_possible_duplicate", "duplicate_existing_ids", "error",
    ):
        assert key in row
    assert row["status"] == QUEUE_STATUS_READY


# 2. MISSING PO -> Needs Review
def test_batch_missing_po_yields_needs_review(tmp_path, monkeypatch):
    monkeypatch.setenv("OILOPS_DOCS_DIR", str(tmp_path / "docs"))
    conn, _ = _init_tmp_db()
    pdf_path = _wrap(CASE_3_LINES, tmp_path, "case3_missing_po.pdf")
    files = [BatchIntakeFile(filename=pdf_path.name, raw_bytes=pdf_path.read_bytes())]
    items = process_pdf_batch(files, conn=conn)
    conn.commit()
    conn.close()
    item = items[0]
    assert item.queue_status == QUEUE_STATUS_NEEDS_REVIEW
    codes = [i.code for i in item.validation.issues]
    assert "MISSING_PO" in codes
    row = queue_row_from_item(item)
    assert row["status"] == QUEUE_STATUS_NEEDS_REVIEW
    assert row["validation"]["warning_count"] >= 1


# 3. DUPLICATE -> Possible Duplicate
def test_batch_duplicate_detection_marks_possible_duplicate(tmp_path, monkeypatch):
    monkeypatch.setenv("OILOPS_DOCS_DIR", str(tmp_path / "docs"))
    conn, _ = _init_tmp_db()
    pdf_a = _wrap(CASE_4_LINES, tmp_path, "case4_dup_a.pdf")
    pdf_b = _wrap(CASE_5_LINES, tmp_path, "case5_dup_b.pdf")
    files = [
        BatchIntakeFile(filename=pdf_a.name, raw_bytes=pdf_a.read_bytes()),
        BatchIntakeFile(filename=pdf_b.name, raw_bytes=pdf_b.read_bytes()),
    ]
    items = process_pdf_batch(files, conn=conn)
    conn.commit()
    conn.close()
    assert len(items) == 2
    first = items[0]
    # case4 due 2026-09-19 vs reference 2026-09-22 is past-due, so the
    # first upload can legitimately be Needs Review. The duplicate
    # test only requires that the FIRST upload is NOT flagged as
    # Possible Duplicate, and the SECOND upload IS flagged and is
    # still saved with its own id.
    assert first.is_possible_duplicate is False
    assert first.queue_status in (
        QUEUE_STATUS_READY, QUEUE_STATUS_NEEDS_REVIEW,
    )
    assert first.invoice_id is not None
    second = items[1]
    assert second.is_possible_duplicate is True
    assert second.queue_status == QUEUE_STATUS_POSSIBLE_DUPLICATE
    assert second.invoice_id is not None
    assert second.invoice_id != first.invoice_id
    assert second.duplicate_existing_ids


# 4. AMOUNT MISMATCH -> Needs Review
def test_batch_amount_mismatch_yields_needs_review(tmp_path, monkeypatch):
    monkeypatch.setenv("OILOPS_DOCS_DIR", str(tmp_path / "docs"))
    conn, _ = _init_tmp_db()
    pdf = _wrap(CASE_8_LINES, tmp_path, "case8_invalid.pdf")
    files = [BatchIntakeFile(filename=pdf.name, raw_bytes=pdf.read_bytes())]
    items = process_pdf_batch(files, conn=conn)
    conn.commit()
    conn.close()
    item = items[0]
    assert item.queue_status == QUEUE_STATUS_NEEDS_REVIEW
    codes = [i.code for i in item.validation.issues]
    assert "AMOUNT_MISMATCH" in codes


# 5. PARSING FAILURE -> Failed (failure isolation)
def test_batch_parsing_failure_is_isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("OILOPS_DOCS_DIR", str(tmp_path / "docs"))
    conn, _ = _init_tmp_db()
    pdf = _wrap(CASE_1_LINES, tmp_path, "case1_clean.pdf")
    files = [
        BatchIntakeFile(filename="not_a_pdf.pdf", raw_bytes=b""),
        BatchIntakeFile(filename="empty.pdf", raw_bytes=b""),
        BatchIntakeFile(filename=pdf.name, raw_bytes=pdf.read_bytes()),
    ]
    items = process_pdf_batch(files, conn=conn)
    conn.commit()
    conn.close()
    assert len(items) == 3
    bad1, bad2, good = items
    assert bad1.queue_status == QUEUE_STATUS_FAILED
    assert bad1.error
    assert bad2.queue_status == QUEUE_STATUS_FAILED
    assert bad2.error
    assert bad2.parse_status == "empty"
    assert good.queue_status == QUEUE_STATUS_READY
    assert good.invoice_id is not None


def test_batch_zero_bytes_payload_is_marked_failed(tmp_path, monkeypatch):
    monkeypatch.setenv("OILOPS_DOCS_DIR", str(tmp_path / "docs"))
    conn, _ = _init_tmp_db()
    files = [BatchIntakeFile(filename="zero.pdf", raw_bytes=b"")]
    items = process_pdf_batch(files, conn=conn)
    conn.commit()
    conn.close()
    assert items[0].queue_status == QUEUE_STATUS_FAILED
    assert items[0].parse_status == "empty"
    assert items[0].error


def test_batch_non_bytes_payload_is_marked_failed(tmp_path, monkeypatch):
    monkeypatch.setenv("OILOPS_DOCS_DIR", str(tmp_path / "docs"))
    conn, _ = _init_tmp_db()
    files = [BatchIntakeFile(filename="junk.pdf", raw_bytes="not bytes")]  # type: ignore[arg-type]
    items = process_pdf_batch(files, conn=conn)
    conn.commit()
    conn.close()
    assert items[0].queue_status == QUEUE_STATUS_FAILED
    assert items[0].error


def test_batch_classification_payload_omits_pdf_bytes(tmp_path, monkeypatch):
    monkeypatch.setenv("OILOPS_DOCS_DIR", str(tmp_path / "docs"))
    conn, _ = _init_tmp_db()
    pdf = _wrap(CASE_1_LINES, tmp_path, "case1_clean.pdf")
    files = [BatchIntakeFile(filename=pdf.name, raw_bytes=pdf.read_bytes())]
    items = process_pdf_batch(files, conn=conn)
    conn.commit()
    conn.close()
    item = items[0]
    assert item.classification is not None
    allowed_keys = {
        "category", "confidence", "reason", "source",
        "used_provider", "is_low_confidence",
    }
    actual = set(item.classification.as_dict().keys())
    assert actual.issubset(allowed_keys)
    forbidden = {"raw_text", "redacted_text", "raw_bytes"}
    assert forbidden.isdisjoint(actual)


def test_batch_document_storage_writes_under_docs_root(tmp_path, monkeypatch):
    monkeypatch.setenv("OILOPS_DOCS_DIR", str(tmp_path / "docs"))
    conn, _ = _init_tmp_db()
    pdf = _wrap(CASE_1_LINES, tmp_path, "case1_clean.pdf")
    files = [BatchIntakeFile(filename=pdf.name, raw_bytes=pdf.read_bytes())]
    items = process_pdf_batch(files, conn=conn)
    conn.commit()
    conn.close()
    stored = items[0].document
    assert stored is not None
    assert stored.document_path.startswith("invoice_" + str(items[0].invoice_id) + "__")
    assert stored.document_size_bytes == len(pdf.read_bytes())
    abs_path = tmp_path / "docs" / stored.document_path
    assert abs_path.exists()
    assert abs_path.read_bytes() == pdf.read_bytes()


def test_batch_failure_does_not_touch_docs_root(tmp_path, monkeypatch):
    monkeypatch.setenv("OILOPS_DOCS_DIR", str(tmp_path / "docs"))
    conn, _ = _init_tmp_db()
    files = [BatchIntakeFile(filename="empty.pdf", raw_bytes=b"")]
    items = process_pdf_batch(files, conn=conn)
    conn.commit()
    conn.close()
    assert items[0].queue_status == QUEUE_STATUS_FAILED
    assert items[0].document is None
    docs_root = tmp_path / "docs"
    pdfs = list(docs_root.glob("invoice_*.pdf"))
    assert pdfs == []


def test_batch_returns_one_item_per_input_file_in_input_order(tmp_path, monkeypatch):
    monkeypatch.setenv("OILOPS_DOCS_DIR", str(tmp_path / "docs"))
    conn, _ = _init_tmp_db()
    pdf_a = _wrap(CASE_1_LINES, tmp_path, "case1.pdf")
    pdf_b = _wrap(CASE_3_LINES, tmp_path, "case3.pdf")
    pdf_c = _wrap(CASE_8_LINES, tmp_path, "case8.pdf")
    files = [
        BatchIntakeFile(filename=pdf_a.name, raw_bytes=pdf_a.read_bytes()),
        BatchIntakeFile(filename=pdf_b.name, raw_bytes=pdf_b.read_bytes()),
        BatchIntakeFile(filename=pdf_c.name, raw_bytes=pdf_c.read_bytes()),
    ]
    items = process_pdf_batch(files, conn=conn)
    conn.commit()
    conn.close()
    assert [it.filename for it in items] == ["case1.pdf", "case3.pdf", "case8.pdf"]
    statuses = [it.queue_status for it in items]
    assert statuses == [QUEUE_STATUS_READY, QUEUE_STATUS_NEEDS_REVIEW, QUEUE_STATUS_NEEDS_REVIEW]


def test_batch_max_pdf_bytes_constant_is_sane():
    assert BATCH_MAX_PDF_BYTES > 0
    from services.invoice_parser import MAX_PDF_BYTES
    assert BATCH_MAX_PDF_BYTES == MAX_PDF_BYTES


def test_batch_intake_does_not_auto_approve_or_pay(tmp_path, monkeypatch):
    monkeypatch.setenv("OILOPS_DOCS_DIR", str(tmp_path / "docs"))
    conn, _ = _init_tmp_db()
    pdf_a = _wrap(CASE_1_LINES, tmp_path, "case1.pdf")
    pdf_b = _wrap(CASE_3_LINES, tmp_path, "case3.pdf")
    files = [
        BatchIntakeFile(filename=pdf_a.name, raw_bytes=pdf_a.read_bytes()),
        BatchIntakeFile(filename=pdf_b.name, raw_bytes=pdf_b.read_bytes()),
    ]
    items = process_pdf_batch(files, conn=conn)
    conn.commit()
    # payment_state is a computed property (status + paid_at); the
    # schema does NOT store it as a column. Verify the persistence
    # invariants directly: status=pending and paid_at IS NULL.
    cur = conn.execute("SELECT id, status, paid_at FROM invoices ORDER BY id")
    rows = cur.fetchall()
    conn.close()
    assert len(rows) == 2
    for row in rows:
        assert row["status"] == "pending"
        assert row["paid_at"] is None


def test_queue_row_from_item_contains_no_sensitive_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("OILOPS_DOCS_DIR", str(tmp_path / "docs"))
    conn, _ = _init_tmp_db()
    pdf = _wrap(CASE_1_LINES, tmp_path, "case1.pdf")
    files = [BatchIntakeFile(filename=pdf.name, raw_bytes=pdf.read_bytes())]
    items = process_pdf_batch(files, conn=conn)
    conn.commit()
    conn.close()
    row = queue_row_from_item(items[0])
    forbidden = {"raw_text", "redacted_text", "redactions", "raw_bytes"}
    assert forbidden.isdisjoint(set(row.keys()))
