"""Batch 4 - Bookkeeping CSV Export.

DESIGN PRINCIPLES
-----------------
1. NO AI / LLM involvement. CSV is built from already-persisted
   InvoiceExtraction fields validated by the deterministic pipeline.
   No arithmetic is performed here.
2. CAD / USD are PRESERVED. No FX conversion, no cross-currency sums.
3. NO sensitive / internal fields are exported: raw_text,
   extraction_json, validation_flags, document_path, sha256,
   size_bytes, reviewer, review_note, payment_note, source_filename,
   classification_source, classification JSON, internal ids.
4. The export is a function of the same filter parameters that
   ``search_tracker`` accepts. It uses the SAME SQL builder so
   "what I see is what I export".
5. Empty result is a valid use case and MUST NOT raise.
6. Thin wrapper around the stdlib ``csv`` module so the file can be
   opened directly by Excel / Sheets / a bookkeeper tool without
   any post-processing.
"""

from __future__ import annotations

import csv
import io
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .invoice_tracker import (
    _build_where,
    _coerce_category,
    _coerce_currency,
    _coerce_due_state,
    _coerce_payment_filter,
    _coerce_review_filter,
    _due_state_for,
    DUE_STATE_ALL,
)


# CSV header columns - keep order stable for bookkeepers
CSV_COLUMNS = (
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
)

# Internal DB column allow-list
_DB_SELECT_COLUMNS = (
    "vendor_name",
    "invoice_number",
    "invoice_date",
    "due_date",
    "po_number",
    "expense_category",
    "description",
    "subtotal",
    "gst",
    "total_amount",
    "currency",
    "status",
    "paid_at",
)

_COLUMN_MAP = {
    "vendor_name": "Vendor",
    "invoice_number": "Invoice Number",
    "invoice_date": "Invoice Date",
    "due_date": "Due Date",
    "po_number": "PO Number",
    "expense_category": "Category",
    "description": "Description",
    "subtotal": "Subtotal",
    "gst": "GST",
    "total_amount": "Total",
    "currency": "Currency",
    "status": "Review Status",
    "paid_at": "Payment Status",
}


class CsvExportError(Exception):
    pass


class InvalidCsvColumnError(CsvExportError):
    pass

def _payment_status_from(status, paid_at):
    st = (status or "").strip()
    pa = paid_at
    if st == "rejected":
        return "Not payable"
    if st == "pending":
        return "Not ready"
    if st == "approved" and pa:
        return "Paid"
    if st == "approved":
        return "Outstanding"
    return "Unknown"


def _fmt_amount(v):
    if v is None:
        return ""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return ""
    return "%.2f" % f


def _fmt_text(v):
    if v is None:
        return ""
    s = str(v)
    return s.strip()


def _row_to_csv(row):
    out = []
    for db_col in _DB_SELECT_COLUMNS:
        value = row[db_col]
        if db_col == "paid_at":
            out.append(_payment_status_from(row["status"], value))
        elif db_col in ("subtotal", "gst", "total_amount"):
            out.append(_fmt_amount(value))
        elif db_col == "status":
            out.append(_fmt_text(value).capitalize() or "")
        else:
            out.append(_fmt_text(value))
    return out


def csv_columns():
    return CSV_COLUMNS


def build_tracker_csv(
    conn,
    *,
    search=None,
    review_filter=None,
    payment_filter=None,
    currency=None,
    category=None,
    due_state=None,
    reference_date=None,
    limit=5000,
):
    p_review = _coerce_review_filter(review_filter or "All")
    p_payment = _coerce_payment_filter(payment_filter or "All")
    p_currency = _coerce_currency(currency)
    p_category = _coerce_category(category)
    p_due_state = _coerce_due_state(due_state or DUE_STATE_ALL)
    p_search = (search or "").strip()

    where_sql, params = _build_where(
        review_filter=p_review,
        payment_filter=p_payment,
        currency=p_currency,
        category=p_category,
        search=p_search,
    )

    select_cols = ", ".join(_DB_SELECT_COLUMNS)
    p_limit = max(1, min(int(limit), 5000))
    sql = (
        "SELECT " + select_cols + " FROM invoices"
        + where_sql
        + " ORDER BY id DESC LIMIT ?"
    )
    cur = conn.execute(sql, tuple(params) + (p_limit,))
    rows = list(cur.fetchall())

    if p_due_state != DUE_STATE_ALL:
        rows = [
            r for r in rows
            if _due_state_for(r["due_date"], reference_date) == p_due_state
        ]

    return render_csv(rows)


def render_csv(rows):
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(list(CSV_COLUMNS))
    for row in rows:
        writer.writerow(_row_to_csv(row))
    return buf.getvalue()


def assert_no_sensitive_columns(csv_text):
    forbidden_substrings = (
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
        "Bank Account",
        "Routing",
        "SWIFT",
        "IBAN",
    )
    lower = csv_text.lower()
    for needle in forbidden_substrings:
        if needle.lower() in lower:
            raise CsvExportError(
                "CSV contains forbidden sensitive/internal field: %r"
                % needle
            )


__all__ = [
    "CSV_COLUMNS",
    "CsvExportError",
    "InvalidCsvColumnError",
    "assert_no_sensitive_columns",
    "build_tracker_csv",
    "csv_columns",
    "render_csv",
]
