"""STEP 8 - Office Dashboard deterministic aggregations.

DESIGN PRINCIPLES
-----------------
1. Every number on the dashboard is computed by SQLite / Python from
   persisted invoices. The LLM is NEVER used for arithmetic.
2. Money totals are ALWAYS returned per currency. CAD + USD are NEVER
   added together. The dashboard exposes a Dict[currency -> Decimal]
   so the UI cannot accidentally cross currencies.
3. The dashboard is deterministic. reference_date is an explicit
   parameter so unit tests never depend on the system clock.
4. SQL is parameterised. User-controlled filters (currency, date
   range, status) are mapped through a fixed allow-list before
   reaching SQL.
5. Sensitive raw data is NEVER queried. No raw_text, no
   bank / routing / SWIFT / IBAN / email / phone / payment-instruction
   fields are selected anywhere in this module.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
ALL_STATUSES = (STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED)

CURRENCY_FILTER_ALL = "ALL"
ALLOWED_CURRENCY_FILTERS: Tuple[str, ...] = (CURRENCY_FILTER_ALL, "CAD", "USD")

_FORBIDDEN_DASHBOARD_COLUMNS = (
    "raw_text",
    "bank_account",
    "routing_number",
    "swift",
    "iban",
    "email",
    "phone",
    "payment_instructions",
)


@dataclass
class CurrencyAmounts:
    amounts: Dict[str, Decimal] = field(default_factory=dict)

    @property
    def currencies(self) -> List[str]:
        return sorted(self.amounts.keys())

    def for_currency(self, code: str) -> Decimal:
        return self.amounts.get(code, Decimal("0"))

    def total_in_currencies(self) -> List[Tuple[str, Decimal]]:
        return [(c, self.amounts[c]) for c in self.currencies]

    def is_empty(self) -> bool:
        return len(self.amounts) == 0


@dataclass
class KPICards:
    total_invoices: int = 0
    pending_review: int = 0
    approved: int = 0
    rejected: int = 0
    outstanding_amount: CurrencyAmounts = field(default_factory=CurrencyAmounts)
    paid_amount: CurrencyAmounts = field(default_factory=CurrencyAmounts)
    due_this_week: int = 0
    overdue: int = 0
    possible_duplicate_groups: int = 0


@dataclass
class NeedsAttentionItem:
    invoice_id: int
    vendor_name: Optional[str]
    invoice_number: Optional[str]
    due_date: Optional[str]
    total_amount: Optional[Decimal]
    currency: Optional[str]
    status: str
    payment_state_marker: Optional[str]
    reasons: List[str]


@dataclass
class CategorySpend:
    category: str
    invoice_count: int
    total_amount: Decimal
    currency: str


@dataclass
class VendorSpend:
    vendor_name: str
    invoice_count: int
    total_amount: Decimal
    currency: str


@dataclass
class DuplicateGroup:
    vendor_name: Optional[str]
    invoice_number: Optional[str]
    total_amount: Optional[Decimal]
    invoice_count: int


@dataclass
class RecentInvoice:
    id: int
    vendor_name: Optional[str]
    invoice_number: Optional[str]
    category: Optional[str]
    status: str
    payment_state_marker: Optional[str]
    total_amount: Optional[Decimal]
    currency: Optional[str]
    created_at: Optional[str]


@dataclass
class DashboardSnapshot:
    reference_date: date
    currency_filter: str
    kpis: KPICards
    needs_attention: List[NeedsAttentionItem]
    spend_by_category: List[CategorySpend]
    spend_by_vendor: List[VendorSpend]
    duplicate_groups: List[DuplicateGroup]
    recent_invoices: List[RecentInvoice]
    available_currencies: List[str]


def _coerce_currency_filter(value: Optional[str]) -> str:
    if value is None:
        return CURRENCY_FILTER_ALL
    v = str(value).strip().upper()
    if v not in ALLOWED_CURRENCY_FILTERS:
        return CURRENCY_FILTER_ALL
    return v


def _to_decimal(v) -> Optional[Decimal]:
    if v is None:
        return None
    if isinstance(v, Decimal):
        return v
    if isinstance(v, (int, float)):
        try:
            return Decimal(str(v))
        except Exception:
            return None
    s = str(v).strip().replace(",", "")
    if not s:
        return None
    try:
        return Decimal(s)
    except Exception:
        return None


def _safe_row_total(row, key) -> Optional[Decimal]:
    try:
        v = row[key]
    except (KeyError, IndexError):
        return None
    return _to_decimal(v)


def _currency_clause(filter_value: str) -> Tuple[str, tuple]:
    if filter_value == CURRENCY_FILTER_ALL:
        return "", ()
    return " AND currency = ?", (filter_value,)


def _kpi_counts(conn):
    cur = conn.execute(
        "SELECT "
        "  COUNT(*) AS total, "
        "  SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) AS pending, "
        "  SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) AS approved, "
        "  SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) AS rejected "
        "FROM invoices",
        (STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED),
    )
    row = cur.fetchone()
    if row is None:
        return 0, 0, 0, 0

    def _i(v):
        return int(v or 0)

    return (
        _i(row["total"]),
        _i(row["pending"]),
        _i(row["approved"]),
        _i(row["rejected"]),
    )


def _kpi_money_by_currency(conn, where_clause, params=()):
    cur = conn.execute(
        "SELECT currency, SUM(total_amount) AS total "
        "FROM invoices " + where_clause + " "
        "GROUP BY currency",
        params,
    )
    out = CurrencyAmounts()
    for row in cur.fetchall():
        ccy = row["currency"]
        total = _to_decimal(row["total"])
        if ccy is None or total is None:
            continue
        out.amounts[str(ccy)] = total
    return out


def _kpi_outstanding(conn, currency_filter=CURRENCY_FILTER_ALL):
    ccy_clause, ccy_params = _currency_clause(currency_filter)
    return _kpi_money_by_currency(
        conn,
        "WHERE status = ? AND paid_at IS NULL" + ccy_clause,
        (STATUS_APPROVED,) + ccy_params,
    )


def _kpi_paid(conn, currency_filter=CURRENCY_FILTER_ALL):
    ccy_clause, ccy_params = _currency_clause(currency_filter)
    return _kpi_money_by_currency(
        conn,
        "WHERE status = ? AND paid_at IS NOT NULL" + ccy_clause,
        (STATUS_APPROVED,) + ccy_params,
    )
def _kpi_due_counts(conn, reference_date, currency_filter):
    ccy_clause, ccy_params = _currency_clause(currency_filter)
    week_end = (reference_date + timedelta(days=7)).isoformat()
    base = (
        "SELECT "
        "  SUM(CASE WHEN substr(due_date,1,10) >= ? "
        "           AND substr(due_date,1,10) <= ? "
        "           THEN 1 ELSE 0 END) AS due_this_week, "
        "  SUM(CASE WHEN substr(due_date,1,10) < ? "
        "           THEN 1 ELSE 0 END) AS overdue "
        "FROM invoices "
        "WHERE status != ? AND paid_at IS NULL"
        + ccy_clause
    )
    params = (
        reference_date.isoformat(),
        week_end,
        reference_date.isoformat(),
        STATUS_REJECTED,
    ) + ccy_params
    cur = conn.execute(base, params)
    row = cur.fetchone()
    if row is None:
        return 0, 0
    return int(row["due_this_week"] or 0), int(row["overdue"] or 0)


def _kpi_duplicate_groups(conn, currency_filter):
    cur = conn.execute(
        "SELECT vendor_name, invoice_number, total_amount, COUNT(*) AS c "
        "FROM invoices "
        "GROUP BY vendor_name, invoice_number, total_amount "
        "HAVING COUNT(*) > 1 "
        "ORDER BY c DESC, vendor_name ASC"
    )
    groups = []
    for row in cur.fetchall():
        groups.append(
            DuplicateGroup(
                vendor_name=row["vendor_name"],
                invoice_number=row["invoice_number"],
                total_amount=_to_decimal(row["total_amount"]),
                invoice_count=int(row["c"] or 0),
            )
        )
    _ = currency_filter
    return len(groups), groups


def _spend_by_category(conn, currency_filter):
    ccy_clause, ccy_params = _currency_clause(currency_filter)
    cur = conn.execute(
        "SELECT "
        "  COALESCE(expense_category, 'Uncategorized') AS category, "
        "  currency, "
        "  COUNT(*) AS c, "
        "  SUM(total_amount) AS total "
        "FROM invoices "
        "WHERE status = ?"
        + ccy_clause + " "
        "GROUP BY COALESCE(expense_category, 'Uncategorized'), currency "
        "ORDER BY total DESC",
        (STATUS_APPROVED,) + ccy_params,
    )
    out = []
    for row in cur.fetchall():
        total = _to_decimal(row["total"])
        if total is None:
            continue
        out.append(
            CategorySpend(
                category=str(row["category"]),
                invoice_count=int(row["c"] or 0),
                total_amount=total,
                currency=str(row["currency"]) if row["currency"] else "",
            )
        )
    return out


def _spend_by_vendor(conn, currency_filter, limit=5):
    ccy_clause, ccy_params = _currency_clause(currency_filter)
    cur = conn.execute(
        "SELECT vendor_name, currency, COUNT(*) AS c, SUM(total_amount) AS total "
        "FROM invoices "
        "WHERE status = ?"
        + ccy_clause + " "
        "GROUP BY vendor_name, currency "
        "ORDER BY total DESC "
        "LIMIT ?",
        (STATUS_APPROVED,) + ccy_params + (int(limit),),
    )
    out = []
    for row in cur.fetchall():
        total = _to_decimal(row["total"])
        if total is None:
            continue
        out.append(
            VendorSpend(
                vendor_name=str(row["vendor_name"]) if row["vendor_name"] else "",
                invoice_count=int(row["c"] or 0),
                total_amount=total,
                currency=str(row["currency"]) if row["currency"] else "",
            )
        )
    return out


_REASON_OVERDUE = "overdue"
_REASON_DUE_SOON = "due_soon"
_REASON_PENDING = "pending"
_REASON_DUPLICATE = "duplicate"

_REASON_PRIORITY = {
    _REASON_OVERDUE: 0,
    _REASON_DUE_SOON: 1,
    _REASON_PENDING: 2,
    _REASON_DUPLICATE: 3,
}


def _due_window(reference_date):
    return reference_date.isoformat(), (reference_date + timedelta(days=7)).isoformat()


def _needs_attention(conn, reference_date, currency_filter):
    due_start, due_end = _due_window(reference_date)
    ccy_clause, ccy_params = _currency_clause(currency_filter)

    sql = (
        "SELECT id, vendor_name, invoice_number, due_date, total_amount, "
        "       currency, status, paid_at "
        "FROM invoices "
        "WHERE "
        "    status = ? "
        "    OR (status != ? AND paid_at IS NULL AND substr(due_date,1,10) < ?) "
        "    OR (status != ? AND paid_at IS NULL "
        "        AND substr(due_date,1,10) >= ? "
        "        AND substr(due_date,1,10) <= ?)"
        + ccy_clause
    )
    params = (
        STATUS_PENDING,
        STATUS_REJECTED,
        reference_date.isoformat(),
        STATUS_REJECTED,
        due_start,
        due_end,
    ) + ccy_params

    rows = conn.execute(sql, params).fetchall()

    dup_ids = {
        int(r["id"])
        for r in conn.execute(
            "SELECT id FROM invoices WHERE id IN ("
            "  SELECT id FROM invoices "
            "  GROUP BY vendor_name, invoice_number, total_amount "
            "  HAVING COUNT(*) > 1"
            ")"
        ).fetchall()
    }

    items = []
    for row in rows:
        inv_id = int(row["id"])
        reasons = []
        status = str(row["status"] or "")
        due_iso = (row["due_date"] or "")[:10]
        paid_at = row["paid_at"]

        if status != STATUS_REJECTED and paid_at is None and due_iso:
            try:
                if date.fromisoformat(due_iso) < reference_date:
                    reasons.append(_REASON_OVERDUE)
            except ValueError:
                pass

        if status != STATUS_REJECTED and paid_at is None and due_iso:
            try:
                dd = date.fromisoformat(due_iso)
                delta = (dd - reference_date).days
                if 0 <= delta <= 7:
                    reasons.append(_REASON_DUE_SOON)
            except ValueError:
                pass

        if status == STATUS_PENDING:
            reasons.append(_REASON_PENDING)

        if inv_id in dup_ids:
            reasons.append(_REASON_DUPLICATE)

        if not reasons:
            continue

        payment_marker = None
        if paid_at:
            payment_marker = "paid"
        elif status == STATUS_APPROVED:
            payment_marker = "unpaid"
        elif status == STATUS_REJECTED:
            payment_marker = "rejected"
        elif status == STATUS_PENDING:
            payment_marker = "pending"

        items.append(
            NeedsAttentionItem(
                invoice_id=inv_id,
                vendor_name=row["vendor_name"],
                invoice_number=row["invoice_number"],
                due_date=row["due_date"],
                total_amount=_safe_row_total(row, "total_amount"),
                currency=row["currency"],
                status=status,
                payment_state_marker=payment_marker,
                reasons=reasons,
            )
        )

    def _sort_key(it):
        top = min(_REASON_PRIORITY[r] for r in it.reasons)
        return (top, it.due_date or "9999-99-99", it.invoice_id)

    items.sort(key=_sort_key)
    return items


def _recent_invoices(conn, currency_filter, limit=10):
    ccy_clause, ccy_params = _currency_clause(currency_filter)
    cur = conn.execute(
        "SELECT id, vendor_name, invoice_number, expense_category, status, "
        "       paid_at, total_amount, currency, created_at "
        "FROM invoices "
        "WHERE 1=1" + ccy_clause + " "
        "ORDER BY id DESC LIMIT ?",
        ccy_params + (int(limit),),
    )
    out = []
    for row in cur.fetchall():
        payment_marker = None
        if row["paid_at"]:
            payment_marker = "paid"
        elif row["status"] == STATUS_APPROVED:
            payment_marker = "unpaid"
        elif row["status"] == STATUS_REJECTED:
            payment_marker = "rejected"
        elif row["status"] == STATUS_PENDING:
            payment_marker = "pending"
        out.append(
            RecentInvoice(
                id=int(row["id"]),
                vendor_name=row["vendor_name"],
                invoice_number=row["invoice_number"],
                category=row["expense_category"],
                status=str(row["status"] or ""),
                payment_state_marker=payment_marker,
                total_amount=_safe_row_total(row, "total_amount"),
                currency=row["currency"],
                created_at=row["created_at"],
            )
        )
    return out


def _available_currencies(conn):
    cur = conn.execute(
        "SELECT DISTINCT currency FROM invoices "
        "WHERE currency IS NOT NULL AND TRIM(currency) != '' "
        "ORDER BY currency"
    )
    return [str(r["currency"]) for r in cur.fetchall()]


def compute_dashboard(conn, reference_date, currency=None):
    currency_filter = _coerce_currency_filter(currency)
    total, pending, approved, rejected = _kpi_counts(conn)
    outstanding = _kpi_outstanding(conn, currency_filter)
    paid = _kpi_paid(conn, currency_filter)
    due_week, overdue = _kpi_due_counts(conn, reference_date, currency_filter)
    dup_count, dup_groups = _kpi_duplicate_groups(conn, currency_filter)
    category = _spend_by_category(conn, currency_filter)
    vendors = _spend_by_vendor(conn, currency_filter, limit=5)
    attention = _needs_attention(conn, reference_date, currency_filter)
    recent = _recent_invoices(conn, currency_filter, limit=10)
    currencies = _available_currencies(conn)
    kpis = KPICards(
        total_invoices=total, pending_review=pending, approved=approved, rejected=rejected,
        outstanding_amount=outstanding, paid_amount=paid,
        due_this_week=due_week, overdue=overdue, possible_duplicate_groups=dup_count,
    )
    return DashboardSnapshot(
        reference_date=reference_date, currency_filter=currency_filter, kpis=kpis,
        needs_attention=attention, spend_by_category=category, spend_by_vendor=vendors,
        duplicate_groups=dup_groups, recent_invoices=recent, available_currencies=currencies,
    )

__all__ = [
    "ALLOWED_CURRENCY_FILTERS", "ALL_STATUSES", "CategorySpend",
    "CURRENCY_FILTER_ALL", "CurrencyAmounts", "DashboardSnapshot",
    "DuplicateGroup", "KPICards", "NeedsAttentionItem",
    "RecentInvoice", "STATUS_APPROVED", "STATUS_PENDING",
    "STATUS_REJECTED", "VendorSpend", "compute_dashboard",
]
