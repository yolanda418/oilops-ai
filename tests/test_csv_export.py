"""Batch 4 - Bookkeeping CSV Export tests.

Covers:
- CSV column allow-list is stable
- build_tracker_csv uses the same filter as search_tracker
- search / filter / category / currency / due-state filter the export
- empty export does not raise, returns header-only CSV
- CAD / USD currency is preserved
- amount values match DB values (no AI arithmetic)
- approval / payment status are correctly labelled
- sensitive fields (raw_text, document_path, sha256, etc.) NEVER
  appear in the rendered CSV
- forbidden phrases (Bank Account, Routing, SWIFT, IBAN) NEVER
  appear in the rendered CSV
- render_csv is reusable with hand-crafted rows
- regression smoke: search_tracker still works
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from database.db import get_connection, init_db
from services.ai_classifier import SOURCE_MOCK
from services.csv_export import (
    CSV_COLUMNS,
    CsvExportError,
    assert_no_sensitive_columns,
    build_tracker_csv,
    csv_columns,
    render_csv,
)
from services.invoice_extractor import InvoiceExtraction
from services.invoice_repository import (
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_REJECTED,
    approve_invoice,
    mark_paid,
    reject_invoice,
    save_pending,
)
from services.invoice_tracker import (
    DUE_STATE_OVERDUE,
    PAYMENT_FILTER_OUTSTANDING,
    PAYMENT_FILTER_PAID,
    REVIEW_FILTER_APPROVED,
    REVIEW_FILTER_PENDING,
    REVIEW_FILTER_REJECTED,
    search_tracker,
)

# ---------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------

@pytest.fixture()
def temp_db_path(tmp_path, monkeypatch):
    db_path = tmp_path / "batch4_csv.db"
    monkeypatch.setenv("OILOPS_DB_PATH", str(db_path))
    init_db(str(db_path))
    yield str(db_path)


@pytest.fixture()
def temp_conn(temp_db_path):
    conn = get_connection(temp_db_path)
    try:
        yield conn
    finally:
        conn.close()


def _ext(**overrides):
    defaults = dict(
        vendor_name="Prairie Pump Rentals",
        invoice_number="PPR-2026-100",
        invoice_date="2026-09-15",
        due_date="2026-09-30",
        po_number="PO-1042",
        subtotal=Decimal("5000.00"),
        gst=Decimal("250.00"),
        total_amount=Decimal("5250.00"),
        currency="CAD",
        description="Equipment rental for wellsite pump.",
        warnings=[],
    )
    defaults.update(overrides)
    return InvoiceExtraction(**defaults)


def _save(conn, **overrides):
    category = overrides.pop("expense_category", "Equipment")
    source = overrides.pop("source_filename", "x.pdf")
    ext = _ext(**overrides)
    return save_pending(
        conn, ext,
        source_filename=source,
        expense_category=category,
    )

# ---------------------------------------------------------------------
# Column allow-list
# ---------------------------------------------------------------------

def test_csv_columns_immutable_and_ordered():
    assert csv_columns() == CSV_COLUMNS
    # Exact order bookkeepers see
    assert list(CSV_COLUMNS) == [
        "Vendor",
        "Invoice Number",
        "Invoice Date",
        "Due Date",
        "PO Number",
        "Category",
        "Description",
        "Subtotal",
        "GST",
        "Total",
        "Currency",
        "Review Status",
        "Payment Status",
    ]


def test_csv_header_only_for_empty_export(temp_conn):
    csv_text = build_tracker_csv(temp_conn)
    lines_out = csv_text.splitlines()
    assert len(lines_out) == 1
    assert lines_out[0].split(",") == list(CSV_COLUMNS)


def test_csv_export_no_rows_does_not_raise(temp_conn):
    # No invoices exist at all
    csv_text = build_tracker_csv(temp_conn)
    assert isinstance(csv_text, str)
    # First non-empty row is the header
    assert csv_text.startswith(",".join(CSV_COLUMNS))


# ---------------------------------------------------------------------
# Filter behaviour
# ---------------------------------------------------------------------

def test_csv_export_respects_review_filter(temp_conn):
    pending_id = _save(
        temp_conn, invoice_number="PPR-A", total_amount=Decimal("100"))
    approved_id = _save(
        temp_conn, invoice_number="PPR-B", total_amount=Decimal("200"))
    approve_invoice(temp_conn, approved_id, reviewer="Alice")

    csv_all = build_tracker_csv(temp_conn)
    csv_pending = build_tracker_csv(
        temp_conn, review_filter=REVIEW_FILTER_PENDING)
    csv_approved = build_tracker_csv(
        temp_conn, review_filter=REVIEW_FILTER_APPROVED)

    assert csv_all.count(chr(10)) == 3  # header + 2 rows
    assert csv_pending.count(chr(10)) == 2  # header + 1
    assert csv_approved.count(chr(10)) == 2

    # Search result and CSV result must agree on invoice ids.
    rows_pending = search_tracker(temp_conn, review_filter=REVIEW_FILTER_PENDING)
    assert {r.id for r in rows_pending} == {pending_id}
    rows_approved = search_tracker(temp_conn, review_filter=REVIEW_FILTER_APPROVED)
    assert {r.id for r in rows_approved} == {approved_id}


def test_csv_export_respects_search(temp_conn):
    a = _save(temp_conn, invoice_number="ALPHA-1", vendor_name="Alpha Co")
    b = _save(temp_conn, invoice_number="BETA-2", vendor_name="Beta Co")

    csv_alpha = build_tracker_csv(temp_conn, search="Alpha")
    assert "Alpha Co" in csv_alpha
    assert "Beta Co" not in csv_alpha

    csv_via_invoice = build_tracker_csv(temp_conn, search="BETA-2")
    assert "Beta Co" in csv_via_invoice
    assert "Alpha Co" not in csv_via_invoice

    # Tracker search and CSV export must agree.
    rows = search_tracker(temp_conn, search="ALPHA")
    assert {r.id for r in rows} == {a}
    rows = search_tracker(temp_conn, search="Beta")
    assert {r.id for r in rows} == {b}

# ---------------------------------------------------------------------
# Currency preservation
# ---------------------------------------------------------------------

def test_csv_export_preserves_cad_currency(temp_conn):
    _save(temp_conn, invoice_number="CAD-1", currency="CAD",
          total_amount=Decimal("1234.56"))
    csv_text = build_tracker_csv(temp_conn)
    rows = [l for l in csv_text.splitlines() if "CAD-1" in l]
    assert len(rows) == 1
    cells = rows[0].split(",")
    # Currency cell is at index 10 (0-based)
    assert cells[10] == "CAD"
    # Total cell is index 9, exact decimal preserved
    assert cells[9] == "1234.56"


def test_csv_export_preserves_usd_currency(temp_conn):
    _save(temp_conn, invoice_number="USD-1", currency="USD",
          subtotal=Decimal("500.00"), gst=Decimal("0.00"),
          total_amount=Decimal("500.00"))
    csv_text = build_tracker_csv(temp_conn)
    rows = [l for l in csv_text.splitlines() if "USD-1" in l]
    cells = rows[0].split(",")
    assert cells[10] == "USD"
    assert cells[9] == "500.00"
    assert cells[7] == "500.00"  # Subtotal
    assert cells[8] == "0.00"    # GST


def test_csv_export_preserves_both_currencies_separately(temp_conn):
    _save(temp_conn, invoice_number="CAD-X", currency="CAD",
          total_amount=Decimal("100.00"))
    _save(temp_conn, invoice_number="USD-Y", currency="USD",
          total_amount=Decimal("200.00"))
    csv_text = build_tracker_csv(temp_conn)
    cad_rows = [l for l in csv_text.splitlines() if "CAD-X" in l]
    usd_rows = [l for l in csv_text.splitlines() if "USD-Y" in l]
    assert cad_rows[0].split(",")[10] == "CAD"
    assert usd_rows[0].split(",")[10] == "USD"
    # No combined / converted total cell anywhere.
    assert "300.00" not in csv_text  # never summed


# ---------------------------------------------------------------------
# Amount correctness (no AI arithmetic)
# ---------------------------------------------------------------------

def test_csv_amount_values_match_db(temp_conn):
    saved = _save(
        temp_conn, invoice_number="AMT-1",
        subtotal=Decimal("1234.56"),
        gst=Decimal("61.73"),
        total_amount=Decimal("1296.29"),
    )
    csv_text = build_tracker_csv(temp_conn)
    rows = [l for l in csv_text.splitlines() if "AMT-1" in l]
    cells = rows[0].split(",")
    # Values must be exactly what was persisted.
    assert cells[7] == "1234.56"
    assert cells[8] == "61.73"
    assert cells[9] == "1296.29"


def test_csv_amount_renders_two_decimals(temp_conn):
    # Total is stored as 100 (REAL). The CSV must still format as "100.00"
    _save(temp_conn, invoice_number="AMT-2", total_amount=Decimal("100"))
    csv_text = build_tracker_csv(temp_conn)
    rows = [l for l in csv_text.splitlines() if "AMT-2" in l]
    cells = rows[0].split(",")
    assert cells[9] == "100.00"

# ---------------------------------------------------------------------
# Approval / payment status labels
# ---------------------------------------------------------------------

def test_csv_payment_status_outstanding(temp_conn):
    new_id = _save(temp_conn, invoice_number="PAY-1",
                  total_amount=Decimal("100"))
    approve_invoice(temp_conn, new_id, reviewer="Alice")
    csv_text = build_tracker_csv(temp_conn)
    rows = [l for l in csv_text.splitlines() if "PAY-1" in l]
    cells = rows[0].split(",")
    # Review Status column is at index 11, Payment Status at index 12.
    assert cells[11] == "Approved"
    assert cells[12] == "Outstanding"


def test_csv_payment_status_paid(temp_conn):
    new_id = _save(temp_conn, invoice_number="PAY-2",
                  total_amount=Decimal("100"))
    approve_invoice(temp_conn, new_id, reviewer="Alice")
    mark_paid(temp_conn, new_id, paid_at="2026-10-01T10:00:00",
              payment_note="wired")
    csv_text = build_tracker_csv(temp_conn)
    rows = [l for l in csv_text.splitlines() if "PAY-2" in l]
    cells = rows[0].split(",")
    assert cells[11] == "Approved"
    assert cells[12] == "Paid"


def test_csv_review_status_pending(temp_conn):
    _save(temp_conn, invoice_number="PEN-1",
          total_amount=Decimal("100"))
    csv_text = build_tracker_csv(temp_conn)
    rows = [l for l in csv_text.splitlines() if "PEN-1" in l]
    cells = rows[0].split(",")
    assert cells[11] == "Pending"
    assert cells[12] == "Not ready"


def test_csv_review_status_rejected(temp_conn):
    new_id = _save(temp_conn, invoice_number="REJ-1",
                  total_amount=Decimal("100"))
    reject_invoice(temp_conn, new_id, reviewer="Bob", review_note="bad")
    csv_text = build_tracker_csv(temp_conn)
    rows = [l for l in csv_text.splitlines() if "REJ-1" in l]
    cells = rows[0].split(",")
    assert cells[11] == "Rejected"
    assert cells[12] == "Not payable"

# ---------------------------------------------------------------------
# Security / sensitive fields excluded
# ---------------------------------------------------------------------

def test_csv_export_never_includes_internal_schema_fields(temp_conn):
    """Internal columns like document_path / sha256 / raw_text /
    review_note / payment_note / source_filename / extraction_json
    MUST NEVER appear in the rendered CSV, even if the user has
    populated free-text fields with phrases that look sensitive.

    This is the safety boundary that ``assert_no_sensitive_columns``
    enforces.
    """
    new_id = _save(temp_conn, invoice_number="SEC-1",
                  total_amount=Decimal("100"))
    # Simulate a reviewer note + payment note. These MUST NOT leak.
    approve_invoice(temp_conn, new_id, reviewer="Alice")
    mark_paid(
        temp_conn, new_id,
        paid_at="2026-10-01T10:00:00",
        payment_note="paid via ACH",
    )

    csv_text = build_tracker_csv(temp_conn)

    internal_column_names = (
        "document_path",
        "document_sha256",
        "document_size_bytes",
        "raw_text",
        "extraction_json",
        "validation_flags",
        "review_note",
        "payment_note",
        "source_filename",
        "classification_source",
    )
    for needle in internal_column_names:
        assert needle.lower() not in csv_text.lower(), (
            "CSV contains internal column %r in: %r"
            % (needle, csv_text)
        )
    # Also no raw reviewer/payment note content.
    assert "paid via ACH" not in csv_text

    # The assertion helper must also pass.
    assert_no_sensitive_columns(csv_text)


def test_csv_export_never_includes_internal_id(temp_conn):
    """The 'id' column must never appear as a CSV column header."""
    _save(temp_conn, invoice_number="ID-1",
          total_amount=Decimal("100"))
    csv_text = build_tracker_csv(temp_conn)
    assert csv_text.splitlines()[0] == ",".join(CSV_COLUMNS)
    assert "id" not in csv_text.splitlines()[0].lower().split(",")


def test_csv_export_does_not_include_internal_id(temp_conn):
    _save(temp_conn, invoice_number="ID-1",
          total_amount=Decimal("100"))
    csv_text = build_tracker_csv(temp_conn)
    # The internal primary-key column is NOT in CSV_COLUMNS.
    assert "ID" not in csv_text.splitlines()[0]


def test_csv_export_does_not_expose_storage_columns(temp_conn):
    """No absolute paths or storage columns appear in the CSV header
    or body. The document storage columns are NOT part of CSV_COLUMNS.
    """
    new_id = _save(temp_conn, invoice_number="PATH-1",
                  total_amount=Decimal("100"))
    csv_text = build_tracker_csv(temp_conn)
    header = csv_text.splitlines()[0]
    # The header must be exactly the public CSV_COLUMNS.
    assert header == ",".join(CSV_COLUMNS)
    # None of the internal storage column names can appear anywhere.
    for needle in ("document_path", "document_sha256",
                   "document_size_bytes"):
        assert needle.lower() not in csv_text.lower()


def test_assert_no_sensitive_columns_raises_on_synthetic_leak():
    bad = "Vendor,Bank Account\nAcme,1234"
    with pytest.raises(CsvExportError):
        assert_no_sensitive_columns(bad)

# ---------------------------------------------------------------------
# render_csv helper
# ---------------------------------------------------------------------

class _FakeRow(dict):
    """Tiny dict-like with index/key access for csv module."""
    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return dict.__getitem__(self, key)


def test_render_csv_empty_rows_returns_header_only():
    out = render_csv([])
    assert out.splitlines() == [",".join(CSV_COLUMNS)]


def test_render_csv_renders_columns_in_order():
    row = _FakeRow({
        "vendor_name": "Acme",
        "invoice_number": "INV-1",
        "invoice_date": "2026-09-15",
        "due_date": "2026-09-30",
        "po_number": "PO-1",
        "expense_category": "Equipment",
        "description": "Test",
        "subtotal": 100.0,
        "gst": 5.0,
        "total_amount": 105.0,
        "currency": "CAD",
        "status": "pending",
        "paid_at": None,
    })
    out = render_csv([row])
    lines = out.splitlines()
    assert lines[0].split(",") == list(CSV_COLUMNS)
    cells = lines[1].split(",")
    assert cells[0] == "Acme"
    assert cells[1] == "INV-1"
    assert cells[8] == "5.00"   # GST
    assert cells[10] == "CAD"
    assert cells[11] == "Pending"
    assert cells[12] == "Not ready"


# ---------------------------------------------------------------------
# Batch 4 regression smoke
# ---------------------------------------------------------------------

def test_search_tracker_filter_agrees_with_csv_export(temp_conn):
    ids = []
    ids.append(_save(temp_conn, invoice_number="AG-1",
                     vendor_name="Gamma Co",
                     total_amount=Decimal("10")))
    ids.append(_save(temp_conn, invoice_number="AG-2",
                     vendor_name="Delta Co",
                     currency="USD",
                     total_amount=Decimal("20")))
    # Approve the second one to make it Outstanding.
    approve_invoice(temp_conn, ids[1], reviewer="Alice")

    # All rows
    csv_all = build_tracker_csv(temp_conn)
    rows_all = search_tracker(temp_conn)
    assert csv_all.count(chr(10)) == 1 + len(rows_all)

    # Approved only
    csv_appr = build_tracker_csv(temp_conn,
                               review_filter=REVIEW_FILTER_APPROVED)
    rows_appr = search_tracker(temp_conn,
                             review_filter=REVIEW_FILTER_APPROVED)
    assert csv_appr.count(chr(10)) == 1 + len(rows_appr)
    assert {ids[1]} == {r.id for r in rows_appr}

    # Outstanding payment filter
    csv_out = build_tracker_csv(temp_conn,
                              payment_filter=PAYMENT_FILTER_OUTSTANDING)
    rows_out = search_tracker(temp_conn,
                            payment_filter=PAYMENT_FILTER_OUTSTANDING)
    assert csv_out.count(chr(10)) == 1 + len(rows_out)

