"""Batch 3 - Practical Invoice Tracker.

DESIGN PRINCIPLES
-----------------
1. NEVER calls any LLM. The deterministic validator is the only authority
   on amounts, duplicates and due dates.
2. Edits are STRICTLY limited to invoices whose status == 'pending'.
   Approved / paid invoices are read-only.
3. Original PDF is NEVER sent to an LLM. Only structured
   InvoiceExtraction fields are edited; raw text is never persisted.
4. SQL is parameterised. User-controlled filter values are mapped through
   fixed allow-lists before reaching SQL.
5. raw_text is never read, written, or returned.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from .invoice_checker import (
    check_invoice,
    InvoiceValidationResult,
    ValidationIssue,
)
from .invoice_extractor import InvoiceExtraction
from .invoice_repository import (
    NotFoundError,
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_REJECTED,
    SavedInvoice,
    get_invoice,
)

REVIEW_FILTER_ALL = "All"
REVIEW_FILTER_PENDING = "Pending"
REVIEW_FILTER_APPROVED = "Approved"
REVIEW_FILTER_REJECTED = "Rejected"
PAYMENT_FILTER_ALL = "All"
PAYMENT_FILTER_OUTSTANDING = "Outstanding"
PAYMENT_FILTER_PAID = "Paid"
ALL_REVIEW_FILTERS: Tuple[str, ...] = (
    REVIEW_FILTER_ALL,
    REVIEW_FILTER_PENDING,
    REVIEW_FILTER_APPROVED,
    REVIEW_FILTER_REJECTED,
)
ALL_PAYMENT_FILTERS: Tuple[str, ...] = (
    PAYMENT_FILTER_ALL,
    PAYMENT_FILTER_OUTSTANDING,
    PAYMENT_FILTER_PAID,
)

CATEGORY_FILTER_ALL = "All"

DUE_STATE_ALL = "All"
DUE_STATE_NO_DUE_DATE = "No due date"
DUE_STATE_OVERDUE = "Overdue"
DUE_STATE_DUE_SOON = "Due soon"
DUE_STATE_DUE_LATER = "Due later"
ALL_DUE_STATES: Tuple[str, ...] = (
    DUE_STATE_ALL,
    DUE_STATE_NO_DUE_DATE,
    DUE_STATE_OVERDUE,
    DUE_STATE_DUE_SOON,
    DUE_STATE_DUE_LATER,
)

EDITABLE_FIELDS: Tuple[str, ...] = (
    "vendor_name",
    "invoice_number",
    "invoice_date",
    "due_date",
    "po_number",
    "subtotal",
    "gst",
    "total_amount",
    "currency",
    "description",
)

_TRACKER_COLUMNS: Tuple[str, ...] = (
    "id", "source_filename", "vendor_name", "invoice_number",
    "invoice_date", "due_date", "po_number", "description",
    "currency", "subtotal", "gst", "total_amount",
    "expense_category", "classification_source", "status",
    "reviewer", "review_note", "paid_at", "payment_note",
    "created_at", "updated_at",
    "document_path", "document_sha256", "document_size_bytes",
)

class TrackerError(Exception):
    pass


class InvoiceNotEditableError(TrackerError):
    pass


class InvalidEditFieldError(TrackerError):
    pass


class InvalidFilterValueError(TrackerError):
    pass


class InvalidAmountError(TrackerError):
    pass


@dataclass
class TrackerRow:
    id: int
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
    status: str
    payment_state: str
    paid_at: Optional[str]
    document_path: Optional[str]


@dataclass
class TrackerDetail:
    invoice: SavedInvoice
    validation: Optional[InvoiceValidationResult]


def _coerce_review_filter(value):
    v = (value or "").strip() or REVIEW_FILTER_ALL
    if v not in ALL_REVIEW_FILTERS:
        raise InvalidFilterValueError(
            "Unknown review filter: %r" % v
        )
    return v


def _coerce_payment_filter(value):
    v = (value or "").strip() or PAYMENT_FILTER_ALL
    if v not in ALL_PAYMENT_FILTERS:
        raise InvalidFilterValueError(
            "Unknown payment filter: %r" % v
        )
    return v


def _coerce_currency(value):
    if value is None:
        return ""
    s = str(value).strip()
    if not s or s.upper() == "ALL":
        return ""
    u = s.upper()
    if u not in ("CAD", "USD"):
        raise InvalidFilterValueError(
            "Unsupported currency filter: %r. Allowed: ALL, CAD, USD." % s
        )
    return u


def _coerce_category(value):
    if value is None:
        return ""
    s = str(value).strip()
    if not s or s == CATEGORY_FILTER_ALL:
        return ""
    return s


def _coerce_due_state(value):
    v = (value or "").strip() or DUE_STATE_ALL
    if v not in ALL_DUE_STATES:
        raise InvalidFilterValueError(
            "Unknown due-state filter: %r" % v
        )
    return v


def _due_state_for(due_date_str, reference):
    if not due_date_str:
        return DUE_STATE_NO_DUE_DATE
    try:
        d = date.fromisoformat(due_date_str)
    except (TypeError, ValueError):
        return DUE_STATE_NO_DUE_DATE
    if reference is None:
        return DUE_STATE_DUE_LATER
    if d < reference:
        return DUE_STATE_OVERDUE
    if d <= reference + timedelta(days=7):
        return DUE_STATE_DUE_SOON
    return DUE_STATE_DUE_LATER

def _build_where(*, review_filter, payment_filter, currency, category, search):
    clauses: List[str] = []
    params: List[Any] = []

    if review_filter == REVIEW_FILTER_PENDING:
        clauses.append("status = ?")
        params.append(STATUS_PENDING)
    elif review_filter == REVIEW_FILTER_APPROVED:
        clauses.append("status = ?")
        params.append(STATUS_APPROVED)
    elif review_filter == REVIEW_FILTER_REJECTED:
        clauses.append("status = ?")
        params.append(STATUS_REJECTED)

    if payment_filter == PAYMENT_FILTER_OUTSTANDING:
        clauses.append("status = ? AND paid_at IS NULL")
        params.append(STATUS_APPROVED)
    elif payment_filter == PAYMENT_FILTER_PAID:
        clauses.append("status = ? AND paid_at IS NOT NULL")
        params.append(STATUS_APPROVED)

    if currency:
        clauses.append("currency = ?")
        params.append(currency)

    if category:
        clauses.append("expense_category = ?")
        params.append(category)

    if search:
        like = "%" + search.strip() + "%"
        clauses.append(
            "(vendor_name LIKE ? OR invoice_number LIKE ? OR po_number LIKE ?)"
        )
        params.extend([like, like, like])

    where_sql = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where_sql, params


def _row_to_tracker_row(row) -> TrackerRow:
    return TrackerRow(
        id=int(row["id"]),
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
        status=str(row["status"] or ""),
        payment_state=_payment_state_from_row(row),
        paid_at=row["paid_at"],
        document_path=row["document_path"],
    )


def _payment_state_from_row(row) -> str:
    status = str(row["status"] or "")
    paid_at = row["paid_at"]
    if status == STATUS_REJECTED:
        return "Not payable"
    if status == STATUS_PENDING:
        return "Not ready"
    if status == STATUS_APPROVED and paid_at:
        return "Paid"
    if status == STATUS_APPROVED:
        return "Outstanding"
    return "Unknown"


def search_tracker(
    conn,
    *,
    search=None,
    review_filter=REVIEW_FILTER_ALL,
    payment_filter=PAYMENT_FILTER_ALL,
    currency=None,
    category=None,
    due_state=DUE_STATE_ALL,
    reference_date=None,
    limit=500,
):
    p_review = _coerce_review_filter(review_filter)
    p_payment = _coerce_payment_filter(payment_filter)
    p_currency = _coerce_currency(currency)
    p_category = _coerce_category(category)
    p_due_state = _coerce_due_state(due_state)
    p_search = (search or "").strip()

    where_sql, params = _build_where(
        review_filter=p_review,
        payment_filter=p_payment,
        currency=p_currency,
        category=p_category,
        search=p_search,
    )

    cols = ", ".join(_TRACKER_COLUMNS)
    p_limit = max(1, min(int(limit), 5000))
    sql = (
        "SELECT " + cols + " FROM invoices"
        + where_sql
        + " ORDER BY id DESC LIMIT ?"
    )
    cur = conn.execute(sql, tuple(params) + (p_limit,))
    rows = [_row_to_tracker_row(r) for r in cur.fetchall()]

    if p_due_state == DUE_STATE_NO_DUE_DATE:
        return [r for r in rows if not r.due_date]
    if p_due_state != DUE_STATE_ALL:
        return [
            r for r in rows
            if _due_state_for(r.due_date, reference_date) == p_due_state
        ]
    return rows


def get_tracker_detail(conn, invoice_id: int) -> TrackerDetail:
    saved = get_invoice(conn, int(invoice_id))
    validation = _decode_validation_flags(conn, int(invoice_id))
    return TrackerDetail(invoice=saved, validation=validation)


def _decode_validation_flags(conn, invoice_id):
    try:
        cur = conn.execute(
            "SELECT validation_flags FROM invoices WHERE id = ?",
            (int(invoice_id),),
        )
    except sqlite3.Error:
        return None
    row = cur.fetchone()
    if not row:
        return None
    raw = row["validation_flags"]
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    issues = data.get("issues") or []
    decoded = []
    for issue in issues:
        try:
            decoded.append(
                ValidationIssue(
                    code=str(issue.get("code", "")),
                    severity=str(issue.get("severity", "")),
                    message=str(issue.get("message", "")),
                    field=issue.get("field"),
                )
            )
        except Exception:
            continue
    return InvoiceValidationResult(issues=decoded)

def _parse_iso_date_str(s):
    if s is None:
        return None
    if not isinstance(s, str):
        raise InvalidAmountError("Date must be a string in YYYY-MM-DD format.")
    t = s.strip()
    if not t:
        return None
    try:
        d = date.fromisoformat(t)
    except ValueError as exc:
        raise InvalidAmountError(
            "Invalid date %r. Use ISO format YYYY-MM-DD." % s
        ) from exc
    return d.isoformat()


def _parse_money(v):
    if v is None:
        return None
    if isinstance(v, bool):
        raise InvalidAmountError("Amount cannot be a boolean.")
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        t = v.strip().replace(",", "").replace(" ", "")
        if not t:
            return None
        try:
            return float(Decimal(t))
        except (InvalidOperation, ValueError) as exc:
            raise InvalidAmountError("Invalid amount: %r" % v) from exc
    raise InvalidAmountError("Unsupported amount type.")


def _parse_currency(v):
    if v is None:
        return None
    if not isinstance(v, str):
        raise InvalidAmountError("Currency must be a string.")
    t = v.strip()
    if not t:
        return None
    u = t.upper()
    if u not in ("CAD", "USD"):
        raise InvalidAmountError(
            "Unsupported currency: %r. Allowed: CAD, USD." % v
        )
    return u


def _coerce_edit_fields(fields):
    if not isinstance(fields, dict):
        raise InvalidEditFieldError("Edits must be a dict.")
    out: Dict[str, Any] = {}
    for key, raw in fields.items():
        if key not in EDITABLE_FIELDS:
            raise InvalidEditFieldError(
                "Field %r is not editable." % key
            )
        if key in ("invoice_date", "due_date"):
            out[key] = _parse_iso_date_str(raw)
        elif key in ("subtotal", "gst", "total_amount"):
            v = _parse_money(raw)
            out[key] = Decimal(str(v)) if v is not None else None
        elif key == "currency":
            out[key] = _parse_currency(raw)
        else:
            if raw is None:
                out[key] = None
            elif isinstance(raw, str):
                t = raw.strip()
                out[key] = t if t else None
            else:
                raise InvalidEditFieldError(
                    "Field %r must be a string or None." % key
                )
    return out


def _build_extraction_from_invoice(saved):
    def _dec(v):
        if v is None or v == "":
            return None
        try:
            return Decimal(str(v))
        except (InvalidOperation, ValueError):
            return None
    return InvoiceExtraction(
        vendor_name=saved.vendor_name,
        invoice_number=saved.invoice_number,
        invoice_date=saved.invoice_date,
        due_date=saved.due_date,
        po_number=saved.po_number,
        description=saved.description,
        currency=saved.currency,
        subtotal=_dec(saved.subtotal),
        gst=_dec(saved.gst),
        total_amount=_dec(saved.total_amount),
        warnings=[],
    )


def _build_extraction_json(extraction) -> str:
    payload: Dict[str, Any] = {}
    allowed = (
        "vendor_name", "invoice_number", "invoice_date", "due_date",
        "po_number", "description", "subtotal", "gst",
        "total_amount", "currency",
    )
    for f in allowed:
        v = getattr(extraction, f, None)
        if v is None:
            continue
        if isinstance(v, Decimal):
            payload[f] = str(v)
        else:
            payload[f] = v
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)

def update_pending_invoice_fields(
    conn,
    invoice_id,
    fields,
    *,
    reference_date=None,
    large_amount_threshold=None,
):
    iid = int(invoice_id)
    fields_clean = _coerce_edit_fields(fields)
    if not fields_clean:
        return get_tracker_detail(conn, iid)

    current = get_invoice(conn, iid)
    if current.status != STATUS_PENDING:
        raise InvoiceNotEditableError(
            "Invoice %d is not editable: status is %r." % (iid, current.status)
        )

    extraction = _build_extraction_from_invoice(current)
    for key, value in fields_clean.items():
        setattr(extraction, key, value)

    validation_result = check_invoice(
        extraction,
        db_conn=conn,
        reference_date=reference_date,
        large_amount_threshold=large_amount_threshold,
    )

    validation_json = json.dumps(
        validation_result.as_dict(), ensure_ascii=False, sort_keys=True
    )

    set_clauses = []
    set_params: List[Any] = []
    for key in EDITABLE_FIELDS:
        if key in fields_clean:
            value = fields_clean[key]
            # Money columns are stored as REAL in SQLite; the sqlite
            # driver does NOT accept Decimal. Coerce here.
            if key in ("subtotal", "gst", "total_amount") and value is not None:
                value = float(value)
            set_clauses.append(key + " = ?")
            set_params.append(value)
    set_clauses.append("validation_flags = ?")
    set_params.append(validation_json)
    set_clauses.append("extraction_json = ?")
    set_params.append(_build_extraction_json(extraction))

    set_params.append(iid)
    set_params.append(STATUS_PENDING)
    sql = (
        "UPDATE invoices SET "
        + ", ".join(set_clauses)
        + " WHERE id = ? AND status = ?"
    )
    cur = conn.execute(sql, tuple(set_params))
    if cur.rowcount < 1:
        try:
            latest = get_invoice(conn, iid)
        except NotFoundError:
            raise NotFoundError("Invoice id %d disappeared mid-update." % iid)
        if latest.status != STATUS_PENDING:
            raise InvoiceNotEditableError(
                "Invoice %d status changed to %r; edit cancelled."
                % (iid, latest.status)
            )
    return get_tracker_detail(conn, iid)


__all__ = [
    "ALL_DUE_STATES",
    "ALL_PAYMENT_FILTERS",
    "ALL_REVIEW_FILTERS",
    "CATEGORY_FILTER_ALL",
    "DUE_STATE_ALL",
    "DUE_STATE_DUE_LATER",
    "DUE_STATE_DUE_SOON",
    "DUE_STATE_NO_DUE_DATE",
    "DUE_STATE_OVERDUE",
    "EDITABLE_FIELDS",
    "InvalidAmountError",
    "InvalidEditFieldError",
    "InvalidFilterValueError",
    "InvoiceNotEditableError",
    "PAYMENT_FILTER_ALL",
    "PAYMENT_FILTER_OUTSTANDING",
    "PAYMENT_FILTER_PAID",
    "REVIEW_FILTER_ALL",
    "REVIEW_FILTER_APPROVED",
    "REVIEW_FILTER_PENDING",
    "REVIEW_FILTER_REJECTED",
    "TrackerDetail",
    "TrackerError",
    "TrackerRow",
    "get_tracker_detail",
    "search_tracker",
    "update_pending_invoice_fields",
]

