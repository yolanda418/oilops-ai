"""Offline acceptance audit for the committed 15 synthetic invoice PDFs."""
from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

import pytest

from database.db import get_connection, init_db
from services.ai_classifier import MockProvider, classify_expense
from services.batch_intake import BatchIntakeFile, process_pdf_batch
from services.invoice_checker import (
    CODE_AMOUNT_MISMATCH,
    CODE_MISSING_DUE_DATE,
    CODE_MISSING_PO,
    CODE_POSSIBLE_DUPLICATE,
)
from services.queue_status import (
    QUEUE_STATUS_NEEDS_REVIEW,
    QUEUE_STATUS_POSSIBLE_DUPLICATE,
    QUEUE_STATUS_READY,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = PROJECT_ROOT / "发票示例"
EXPECTED_FIELDS = (
    "vendor_name",
    "invoice_number",
    "invoice_date",
    "due_date",
    "po_number",
    "subtotal",
    "gst",
    "total_amount",
    "currency",
)


@pytest.fixture
def acceptance_db(tmp_path, monkeypatch):
    db_path = tmp_path / "acceptance.db"
    docs_root = tmp_path / "documents"
    monkeypatch.setenv("OILOPS_DOCS_DIR", str(docs_root))
    init_db(str(db_path))
    conn = get_connection(str(db_path))
    try:
        yield conn, docs_root
    finally:
        conn.close()


def test_15_invoice_acceptance(acceptance_db):
    conn, docs_root = acceptance_db
    with (FIXTURE_DIR / "expected_results.csv").open(
        encoding="utf-8-sig", newline=""
    ) as stream:
        expected_rows = list(csv.DictReader(stream))

    pdfs = sorted(FIXTURE_DIR.glob("*.pdf"))
    assert len(pdfs) == 15
    assert len(expected_rows) == 15
    assert [row["filename"] for row in expected_rows] == [p.name for p in pdfs]

    # Keep due-date statuses deterministic and focus this acceptance run
    # on the four documented fixture exceptions.
    items = process_pdf_batch(
        [BatchIntakeFile(p.name, p.read_bytes()) for p in pdfs],
        conn=conn,
        docs_root=docs_root,
        classify_fn=lambda ext: classify_expense(ext, provider=MockProvider()),
        reference_date=date(2026, 8, 1),
    )
    conn.commit()

    assert len(items) == 15
    assert all(item.parse_status == "ok" for item in items)
    assert all(item.invoice_id is not None for item in items)

    field_count = 0
    for item, expected in zip(items, expected_rows):
        extraction = item.extraction
        for field in EXPECTED_FIELDS:
            actual = getattr(extraction, field)
            expected_value = expected[{
                "vendor_name": "vendor",
                "total_amount": "total",
            }.get(field, field)]
            if expected_value:
                assert actual is not None, f"{item.filename}: {field} missing"
                assert str(actual) == expected_value, (
                    f"{item.filename}: {field}: {actual!s} != {expected_value!r}"
                )
                field_count += 1
            else:
                assert actual is None, f"{item.filename}: {field} should be empty"
        assert item.document is not None
        assert (docs_root / item.document.document_path).is_file()

    assert field_count == 133
    codes = [{issue.code for issue in item.validation.issues} for item in items]
    assert CODE_MISSING_PO in codes[5]
    assert CODE_POSSIBLE_DUPLICATE in codes[6]
    assert CODE_AMOUNT_MISMATCH in codes[7]
    assert CODE_MISSING_DUE_DATE in codes[13]

    statuses = [item.queue_status for item in items]
    assert statuses.count(QUEUE_STATUS_READY) == 11
    assert statuses.count(QUEUE_STATUS_NEEDS_REVIEW) == 3
    assert statuses.count(QUEUE_STATUS_POSSIBLE_DUPLICATE) == 1
    assert all(item.queue_status != "Failed" for item in items)
    assert all(item.classification is not None for item in items)

    # Every row is pending: classification never approves or pays.
    rows = conn.execute(
        "SELECT status, paid_at FROM invoices ORDER BY id"
    ).fetchall()
    assert len(rows) == 15
    assert all(row["status"] == "pending" and row["paid_at"] is None for row in rows)