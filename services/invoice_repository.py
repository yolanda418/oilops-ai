"""STEP 7 - Reviewed-invoice persistence & human approval / payment tracking.
DESIGN PRINCIPLES
-----------------
1. AI never approves, rejects, or marks anything paid. Every status
   transition is invoked ONLY from explicit human UI actions.
2. NO payment execution. mark_paid only records timestamp + note.
3. NO silent duplication rejection. POSSIBLE_DUPLICATE is a
   human-review flag, not a DB constraint.
4. Sensitive raw data is NEVER persisted. raw_text is never stored.
   extraction_json is built from an explicit allow-list.
5. SQL is parameterised everywhere.
6. State transitions are guarded at the SQL layer.

PAYMENT TRACKING
----------------
Approval and payment are SEPARATE concerns. The derived payment
state is computed from status + paid_at:
    status=pending,  paid_at IS NULL     -> Not ready
    status=rejected, paid_at IS NULL     -> Not payable
    status=approved, paid_at IS NULL     -> Outstanding
    status=approved, paid_at IS NOT NULL -> Paid
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from .invoice_extractor import InvoiceExtraction
from .invoice_checker import InvoiceValidationResult

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
ALL_STATUSES = (STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED)
TRACKER_FILTER_ALL = "All"
TRACKER_FILTER_PENDING = "Pending"
TRACKER_FILTER_APPROVED = "Approved"
TRACKER_FILTER_REJECTED = "Rejected"
TRACKER_FILTER_OUTSTANDING = "Outstanding"
TRACKER_FILTER_PAID = "Paid"
ALL_TRACKER_FILTERS = (
    TRACKER_FILTER_ALL, TRACKER_FILTER_PENDING, TRACKER_FILTER_APPROVED,
    TRACKER_FILTER_REJECTED, TRACKER_FILTER_OUTSTANDING, TRACKER_FILTER_PAID,
)

_EXTRACTION_JSON_ALLOWED_FIELDS = (
    "vendor_name", "invoice_number", "invoice_date", "due_date",
    "po_number", "description", "subtotal", "gst",
    "total_amount", "currency",
)
_CLASSIFICATION_META_FIELDS = (
    "category", "source", "confidence", "reason",
    "used_provider", "is_low_confidence",
)

class InvoiceRepositoryError(Exception):
    pass
class InvalidStateTransition(InvoiceRepositoryError):
    pass
class MissingReviewerError(InvoiceRepositoryError):
    pass
class MissingReviewNoteError(InvoiceRepositoryError):
    pass
class NotFoundError(InvoiceRepositoryError):
    pass
class InvalidPaymentState(InvoiceRepositoryError):
    pass

@dataclass
class SavedInvoice:
    id: int
    source_filename: Optional[str]
    vendor_name: Optional[str]
    invoice_number: Optional[str]
    invoice_date: Optional[str]
    due_date: Optional[str]
    po_number: Optional[str]
    description: Optional[str]
    currency: Optional[str]
    subtotal: Optional[float]
    gst: Optional[float]
    total_amount: Optional[float]
    expense_category: Optional[str]
    classification_source: Optional[str]
    status: str
    reviewer: Optional[str]
    review_note: Optional[str]
    paid_at: Optional[str]
    payment_note: Optional[str]
    created_at: str
    updated_at: str
    # STEP 9 - document traceability. Legacy rows have all-None.
    document_path: Optional[str] = None
    document_sha256: Optional[str] = None
    document_size_bytes: Optional[int] = None
    @property
    def payment_state(self) -> str:
        if self.status == STATUS_REJECTED:
            return "Not payable"
        if self.status == STATUS_PENDING:
            return "Not ready"
        if self.status == STATUS_APPROVED and self.paid_at:
            return "Paid"
        if self.status == STATUS_APPROVED:
            return "Outstanding"
        return "Unknown"

def _to_money(v):
    if v is None:
        return None
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "")
    if not s:
        return None
    try:
        return float(Decimal(s))
    except Exception:
        return None

def _build_extraction_json(extraction):
    payload = {}
    for f in _EXTRACTION_JSON_ALLOWED_FIELDS:
        v = getattr(extraction, f, None)
        if v is None:
            continue
        if isinstance(v, Decimal):
            payload[f] = str(v)
        else:
            payload[f] = v
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)

def _build_classification_meta(classification):
    if classification is None:
        return None
    if hasattr(classification, "as_dict") and callable(classification.as_dict):
        try:
            raw = classification.as_dict()
        except Exception:
            return None
    elif isinstance(classification, dict):
        raw = dict(classification)
    else:
        return None
    payload = {}
    for f in _CLASSIFICATION_META_FIELDS:
        if f not in raw:
            continue
        v = raw[f]
        if v is None:
            continue
        if f == "confidence" and not isinstance(v, str):
            try:
                payload[f] = float(v)
            except Exception:
                continue
        else:
            payload[f] = v
    if not payload:
        return None
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)

def _build_validation_flags_json(validation):
    if validation is None:
        return None
    return json.dumps(validation.as_dict(), ensure_ascii=False, sort_keys=True)

def _normalise_paid_at(paid_at):
    if isinstance(paid_at, datetime):
        return paid_at.isoformat(sep="T", timespec="seconds")
    if isinstance(paid_at, date):
        return paid_at.isoformat()
    s = str(paid_at).strip()
    if not s:
        raise InvalidPaymentState("paid_at must be a non-empty value.")
    return s

INVOICE_COLUMNS = (
    "id", "source_filename", "vendor_name", "invoice_number",
    "invoice_date", "due_date", "po_number", "description",
    "currency", "subtotal", "gst", "total_amount",
    "expense_category", "classification_source", "status",
    "reviewer", "review_note", "paid_at", "payment_note",
    "created_at", "updated_at",
    # STEP 9 - document traceability (NULL for legacy rows).
    "document_path", "document_sha256", "document_size_bytes",
)

def _row_to_saved_invoice(row):
    return SavedInvoice(
        id=int(row["id"]),
        source_filename=row["source_filename"],
        vendor_name=row["vendor_name"],
        invoice_number=row["invoice_number"],
        invoice_date=row["invoice_date"],
        due_date=row["due_date"],
        po_number=row["po_number"],
        description=row["description"],
        currency=row["currency"],
        subtotal=row["subtotal"],
        gst=row["gst"],
        total_amount=row["total_amount"],
        expense_category=row["expense_category"],
        classification_source=row["classification_source"],
        status=row["status"],
        reviewer=row["reviewer"],
        review_note=row["review_note"],
        paid_at=row["paid_at"],
        payment_note=row["payment_note"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        # STEP 9 - document traceability (NULL for legacy rows).
        document_path=row["document_path"],
        document_sha256=row["document_sha256"],
        document_size_bytes=row["document_size_bytes"],
    )

def get_invoice(conn, invoice_id):
    cols = ", ".join(INVOICE_COLUMNS)
    cur = conn.execute(
        f"SELECT {cols} FROM invoices WHERE id = ?",
        (int(invoice_id),),
    )
    row = cur.fetchone()
    if row is None:
        raise NotFoundError(f"Invoice id {invoice_id} not found.")
    return _row_to_saved_invoice(row)

def save_pending(
    conn,
    extraction,
    *,
    source_filename=None,
    expense_category=None,
    classification_source=None,
    classification=None,
    validation=None,
    reviewer=None,
    review_note=None,
    # STEP 9 - document traceability. All optional; legacy callers
    # that do not pass these get NULL columns in the DB.
    document_path=None,
    document_sha256=None,
    document_size_bytes=None,
):
    """Insert a reviewed invoice with status='pending'.

    Sensitive fields are NEVER stored. raw_text is forced to NULL.
    extraction_json is built from an explicit allow-list.
    """
    extraction_json = _build_extraction_json(extraction)
    classification_meta = _build_classification_meta(classification)
    validation_flags = _build_validation_flags_json(validation)
    cursor = conn.execute(
        """
        INSERT INTO invoices (
            source_filename, raw_text, extraction_json,
            vendor_name, invoice_number, invoice_date, due_date,
            po_number, description, currency, subtotal, gst, total_amount,
            expense_category, classification_source, validation_flags,
            status, review_note, reviewer,
            document_path, document_sha256, document_size_bytes
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?,
            ?, ?, ?
        )
        """,
        (
            source_filename, None, extraction_json,
            extraction.vendor_name, extraction.invoice_number,
            extraction.invoice_date, extraction.due_date,
            extraction.po_number, extraction.description,
            extraction.currency,
            _to_money(extraction.subtotal), _to_money(extraction.gst),
            _to_money(extraction.total_amount),
            expense_category, classification_source, validation_flags,
            STATUS_PENDING, (review_note or None), (reviewer or None),
            # STEP 9 - document traceability (NULL for legacy callers).
            (document_path or None),
            (document_sha256 or None),
            (document_size_bytes if document_size_bytes is not None else None),
        ),
    )
    return int(cursor.lastrowid)

def update_document_ref(
    conn,
    invoice_id,
    *,
    document_path,
    document_sha256,
    document_size_bytes,
):
    '''STEP 9 - attach document traceability metadata.
    No-op when all three args None; otherwise sets the three columns.
    Returns the refreshed SavedInvoice.'''
    if all(v is None for v in (document_path, document_sha256, document_size_bytes)):
        return get_invoice(conn, invoice_id)
    conn.execute(
        'UPDATE invoices SET document_path=?, document_sha256=?, document_size_bytes=? WHERE id=?',
        ((document_path or None), (document_sha256 or None), (document_size_bytes if document_size_bytes is not None else None), int(invoice_id)),
    )
    return get_invoice(conn, invoice_id)


def approve_invoice(conn, invoice_id, *, reviewer, review_note=None):
    """Transition pending -> approved. Guarded at SQL layer."""
    reviewer_clean = (reviewer or "").strip()
    if not reviewer_clean:
        raise MissingReviewerError(
            "Reviewer name is required to approve an invoice."
        )
    cur = conn.execute(
        """
        UPDATE invoices
        SET status = ?, reviewer = ?, review_note = ?
        WHERE id = ? AND status = ?
        """,
        (STATUS_APPROVED, reviewer_clean, (review_note or None),
         int(invoice_id), STATUS_PENDING),
    )
    if cur.rowcount != 1:
        existing = _safe_fetch_status(conn, invoice_id)
        if existing is None:
            raise NotFoundError(f"Invoice id {invoice_id} not found.")
        raise InvalidStateTransition(
            f"Cannot approve invoice {invoice_id}: status is '{existing}', "
            f"expected '{STATUS_PENDING}'."
        )
    return get_invoice(conn, invoice_id)

def reject_invoice(conn, invoice_id, *, reviewer, review_note):
    """Transition pending -> rejected. Requires reviewer AND review note."""
    reviewer_clean = (reviewer or "").strip()
    if not reviewer_clean:
        raise MissingReviewerError(
            "Reviewer name is required to reject an invoice."
        )
    note_clean = (review_note or "").strip()
    if not note_clean:
        raise MissingReviewNoteError(
            "A review note explaining the rejection is required."
        )
    cur = conn.execute(
        """
        UPDATE invoices
        SET status = ?, reviewer = ?, review_note = ?
        WHERE id = ? AND status = ?
        """,
        (STATUS_REJECTED, reviewer_clean, note_clean,
         int(invoice_id), STATUS_PENDING),
    )
    if cur.rowcount != 1:
        existing = _safe_fetch_status(conn, invoice_id)
        if existing is None:
            raise NotFoundError(f"Invoice id {invoice_id} not found.")
        raise InvalidStateTransition(
            f"Cannot reject invoice {invoice_id}: status is '{existing}', "
            f"expected '{STATUS_PENDING}'."
        )
    return get_invoice(conn, invoice_id)

def mark_paid(conn, invoice_id, *, paid_at, payment_note=None):
    """Record payment completion. NO payment execution.

    Guarded: WHERE id = ? AND status = 'approved' AND paid_at IS NULL
    """
    paid_at_clean = _normalise_paid_at(paid_at)
    cur = conn.execute(
        """
        UPDATE invoices
        SET paid_at = ?, payment_note = ?
        WHERE id = ? AND status = ? AND paid_at IS NULL
        """,
        (paid_at_clean, (payment_note or None), int(invoice_id), STATUS_APPROVED),
    )
    if cur.rowcount != 1:
        existing = _safe_fetch_payment(conn, invoice_id)
        if existing is None:
            raise NotFoundError(f"Invoice id {invoice_id} not found.")
        st, pa = existing
        if st != STATUS_APPROVED:
            raise InvalidPaymentState(
                f"Cannot mark invoice {invoice_id} as paid: status is "
                f"'{st}'. Only 'approved' invoices can be marked paid."
            )
        if pa is not None:
            raise InvalidPaymentState(
                f"Invoice {invoice_id} is already marked as paid at {pa}."
            )
        raise InvalidPaymentState(
            f"Cannot mark invoice {invoice_id} as paid for an unknown reason."
        )
    return get_invoice(conn, invoice_id)

def _safe_fetch_status(conn, invoice_id):
    cur = conn.execute("SELECT status FROM invoices WHERE id = ?", (int(invoice_id),))
    row = cur.fetchone()
    return None if row is None else row["status"]

def _safe_fetch_payment(conn, invoice_id):
    cur = conn.execute(
        "SELECT status, paid_at FROM invoices WHERE id = ?",
        (int(invoice_id),),
    )
    row = cur.fetchone()
    return None if row is None else (row["status"], row["paid_at"])

def list_payment_tracker(conn, *, filter_value=TRACKER_FILTER_ALL, limit=500):
    """Return rows for the Payment Tracker view, applying a simple filter."""
    cols = ", ".join(INVOICE_COLUMNS)
    base_sql = f"SELECT {cols} FROM invoices "
    p_limit = int(limit)
    order = " ORDER BY id DESC LIMIT ?"
    f = (filter_value or TRACKER_FILTER_ALL).strip()
    if f == TRACKER_FILTER_PENDING:
        where = " WHERE status = ?"
        params = (STATUS_PENDING,)
    elif f == TRACKER_FILTER_APPROVED:
        where = " WHERE status = ?"
        params = (STATUS_APPROVED,)
    elif f == TRACKER_FILTER_REJECTED:
        where = " WHERE status = ?"
        params = (STATUS_REJECTED,)
    elif f == TRACKER_FILTER_OUTSTANDING:
        where = " WHERE status = ? AND paid_at IS NULL"
        params = (STATUS_APPROVED,)
    elif f == TRACKER_FILTER_PAID:
        where = " WHERE status = ? AND paid_at IS NOT NULL"
        params = (STATUS_APPROVED,)
    else:
        where = ""
        params = ()
    sql = base_sql + where + order
    cur = conn.execute(sql, params + (p_limit,))
    rows = [_row_to_saved_invoice(r) for r in cur.fetchall()]
    return [
        {
            "id": r.id, "source_filename": r.source_filename,
            "vendor_name": r.vendor_name,
            "invoice_number": r.invoice_number,
            "due_date": r.due_date,
            "total_amount": r.total_amount,
            "currency": r.currency,
            "expense_category": r.expense_category,
            "status": r.status,
            "payment_state": r.payment_state,
            "paid_at": r.paid_at,
        }
        for r in rows
    ]

