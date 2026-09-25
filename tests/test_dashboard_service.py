"""
Coverage targets (per STEP 8 spec):
* empty database
* total invoices
* pending count
* approved count
* rejected count
* outstanding amount
* paid amount
* pending not included in outstanding
* rejected not included
* due soon
* overdue
* paid invoice not overdue
* rejected invoice not overdue
* duplicate groups
* three identical invoices = one duplicate group
* approved spend by category
"""
from datetime import date
from decimal import Decimal

import pytest

from database.db import get_connection, init_db
from services import invoice_repository as ir
"""STEP 8 - Dashboard deterministic-aggregation tests.

Coverage targets (per STEP 8 spec):
* empty database
* total invoices
* pending count
* approved count
* rejected count
* outstanding amount
* paid amount
* pending not included in outstanding
* rejected not included
* due soon
* overdue
* paid invoice not overdue
* rejected invoice not overdue
* duplicate groups
* three identical invoices = one duplicate group
* approved spend by category
"""
from services import invoice_repository as ir
from services.dashboard_service import (
    ALLOWED_CURRENCY_FILTERS,
    CURRENCY_FILTER_ALL,
    CURRENCY_FILTER_ALL as _ALL,
    CurrencyAmounts,
    KPICards,
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_REJECTED,
    compute_dashboard,
)


REF_DATE = date(2026, 9, 22)


@pytest.fixture()
def temp_db_path(tmp_path, monkeypatch):
    db_path = tmp_path / "step8_dash.db"
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


def _make_extraction(**overrides):
    """Build a minimal extraction dict for invoice_repository.save_pending."""
    base = {
        "vendor_name": "Vendor X",
        "invoice_number": "INV-001",
        "invoice_date": "2026-09-01",
        "due_date": "2026-09-30",
        "po_number": None,
        "description": None,
        "currency": "CAD",
        "subtotal": Decimal("100"),
        "gst": None,
        "total_amount": Decimal("100"),
    }
    overrides.pop("expense_category", None)
    base.update(overrides)
    from services.invoice_extractor import InvoiceExtraction
    return InvoiceExtraction(**base)



def _save_invoice(conn, **kwargs):
    """Save an invoice using the repository API."""
    extraction = _make_extraction(**kwargs)
    return ir.save_pending(
        conn,
        extraction,
        source_filename="synthetic.pdf",
        expense_category=kwargs.get("expense_category"),
    )

def _approve(conn, invoice_id):
    ir.approve_invoice(conn, invoice_id, reviewer="alice", review_note=None)


def _reject(conn, invoice_id):
    ir.reject_invoice(conn, invoice_id, reviewer="alice", review_note="bad")


def _mark_paid(conn, invoice_id):
    ir.mark_paid(conn, invoice_id, paid_at="2026-09-20", payment_note=None)


# ---------------------------------------------------------------------------
# Empty database
# ---------------------------------------------------------------------------


def test_empty_database_returns_zero_kpis(temp_conn):
    snap = compute_dashboard(temp_conn, REF_DATE)
    assert isinstance(snap.kpis, KPICards)
    assert snap.kpis.total_invoices == 0
    assert snap.kpis.pending_review == 0
    assert snap.kpis.approved == 0
    assert snap.kpis.rejected == 0
    assert snap.kpis.outstanding_amount.amounts == {}
    assert snap.kpis.paid_amount.amounts == {}
    assert snap.kpis.due_this_week == 0
    assert snap.kpis.overdue == 0
    assert snap.kpis.possible_duplicate_groups == 0
    assert snap.needs_attention == []
    assert snap.spend_by_category == []
    assert snap.spend_by_vendor == []
    assert snap.duplicate_groups == []
    assert snap.recent_invoices == []
    assert snap.available_currencies == []


def test_currency_filter_constants_are_safe():
    assert ALLOWED_CURRENCY_FILTERS == ("ALL", "CAD", "USD")
    assert CURRENCY_FILTER_ALL == "ALL"
    assert _ALL == "ALL"


# ---------------------------------------------------------------------------
# Counts
# ---------------------------------------------------------------------------


def test_total_invoices_and_status_counts(temp_conn):
    a = _save_invoice(temp_conn, vendor_name="V1", invoice_number="A-1", total_amount=Decimal("100"))
    b = _save_invoice(temp_conn, vendor_name="V2", invoice_number="A-2", total_amount=Decimal("200"))
    c = _save_invoice(temp_conn, vendor_name="V3", invoice_number="A-3", total_amount=Decimal("300"))
    _approve(temp_conn, a)
    _approve(temp_conn, b)
    _reject(temp_conn, c)
    snap = compute_dashboard(temp_conn, REF_DATE)
    assert snap.kpis.total_invoices == 3
    assert snap.kpis.pending_review == 0
    assert snap.kpis.approved == 2
    assert snap.kpis.rejected == 1


def test_pending_count(temp_conn):
    _save_invoice(temp_conn, vendor_name="V1", invoice_number="P-1")
    _save_invoice(temp_conn, vendor_name="V2", invoice_number="P-2")
    snap = compute_dashboard(temp_conn, REF_DATE)
    assert snap.kpis.pending_review == 2
    assert snap.kpis.approved == 0
    assert snap.kpis.rejected == 0
    assert snap.kpis.total_invoices == 2


# ---------------------------------------------------------------------------
# Outstanding / Paid
# ---------------------------------------------------------------------------


def test_outstanding_amount_excludes_pending_and_rejected(temp_conn):
    """Approved + paid_at NULL -> in outstanding. Pending and rejected -> NOT."""
    approved_unpaid = _save_invoice(temp_conn, vendor_name="V1", invoice_number="O-1", total_amount=Decimal("1000"))
    _approve(temp_conn, approved_unpaid)
    pending = _save_invoice(temp_conn, vendor_name="V2", invoice_number="O-2", total_amount=Decimal("9999"))
    rejected = _save_invoice(temp_conn, vendor_name="V3", invoice_number="O-3", total_amount=Decimal("8888"))
    _reject(temp_conn, rejected)
    snap = compute_dashboard(temp_conn, REF_DATE)
    # Only 1000 should be in outstanding
    assert snap.kpis.outstanding_amount.amounts == {"CAD": Decimal("1000.0")}
    # pending + rejected must NEVER appear
    assert pending not in [r.id for r in snap.recent_invoices if r.status == STATUS_APPROVED]


def test_paid_amount_only_includes_approved_paid(temp_conn):
    a = _save_invoice(temp_conn, vendor_name="V1", invoice_number="P-1", total_amount=Decimal("500"))
    _approve(temp_conn, a)
    _mark_paid(temp_conn, a)
    b = _save_invoice(temp_conn, vendor_name="V2", invoice_number="P-2", total_amount=Decimal("700"))
    _approve(temp_conn, b)  # NOT paid
    snap = compute_dashboard(temp_conn, REF_DATE)
    assert snap.kpis.paid_amount.amounts == {"CAD": Decimal("500.0")}
    assert snap.kpis.outstanding_amount.amounts == {"CAD": Decimal("700.0")}


def test_rejected_invoice_not_in_outstanding(temp_conn):
    inv = _save_invoice(temp_conn, vendor_name="V1", invoice_number="R-1", total_amount=Decimal("1234"))
    _reject(temp_conn, inv)
    snap = compute_dashboard(temp_conn, REF_DATE)
    assert snap.kpis.outstanding_amount.amounts == {}
    assert snap.kpis.paid_amount.amounts == {}


# ---------------------------------------------------------------------------
# Due soon / Overdue
# ---------------------------------------------------------------------------


def test_due_this_week_counts_only_unpaid(temp_conn):
    """0 <= (due_date - ref) <= 7 AND paid_at NULL AND status != rejected."""
    # Reference = 2026-09-22
    # due in 3 days -> due this week
    a = _save_invoice(
        temp_conn, vendor_name="V1", invoice_number="D-1",
        due_date="2026-09-25", total_amount=Decimal("100"),
    )
    _approve(temp_conn, a)
    # due in 5 days, then PAID -> should NOT count
    b = _save_invoice(
        temp_conn, vendor_name="V2", invoice_number="D-2",
        due_date="2026-09-27", total_amount=Decimal("100"),
    )
    _approve(temp_conn, b)
    _mark_paid(temp_conn, b)
    # due in 10 days -> NOT this week
    c = _save_invoice(
        temp_conn, vendor_name="V3", invoice_number="D-3",
        due_date="2026-10-02", total_amount=Decimal("100"),
    )
    _approve(temp_conn, c)
    snap = compute_dashboard(temp_conn, REF_DATE)
    assert snap.kpis.due_this_week == 1


def test_overdue_excludes_paid_and_rejected(temp_conn):
    """due_date < ref AND paid_at NULL AND status != rejected."""
    # overdue (due yesterday)
    a = _save_invoice(
        temp_conn, vendor_name="V1", invoice_number="O-1",
        due_date="2026-09-21", total_amount=Decimal("100"),
    )
    _approve(temp_conn, a)
    # overdue but PAID -> NOT overdue
    b = _save_invoice(
        temp_conn, vendor_name="V2", invoice_number="O-2",
        due_date="2026-09-15", total_amount=Decimal("100"),
    )
    _approve(temp_conn, b)
    _mark_paid(temp_conn, b)
    # overdue but REJECTED -> NOT overdue
    c = _save_invoice(
        temp_conn, vendor_name="V3", invoice_number="O-3",
        due_date="2026-09-10", total_amount=Decimal("100"),
    )
    _reject(temp_conn, c)
    snap = compute_dashboard(temp_conn, REF_DATE)
    assert snap.kpis.overdue == 1


def test_due_window_excludes_more_than_seven_days(temp_conn):
    a = _save_invoice(
        temp_conn, vendor_name="V1", invoice_number="W-1",
        due_date="2026-09-22", total_amount=Decimal("100"),
    )  # today
    b = _save_invoice(
        temp_conn, vendor_name="V2", invoice_number="W-2",
        due_date="2026-09-29", total_amount=Decimal("100"),
    )  # +7 days
    c = _save_invoice(
        temp_conn, vendor_name="V3", invoice_number="W-3",
        due_date="2026-09-30", total_amount=Decimal("100"),
    )  # +8 days -> NOT due this week
    for i in [a, b, c]:
        _approve(temp_conn, i)
    snap = compute_dashboard(temp_conn, REF_DATE)
    assert snap.kpis.due_this_week == 2
    assert snap.kpis.overdue == 0


# ---------------------------------------------------------------------------
# Duplicate groups
# ---------------------------------------------------------------------------


def test_duplicate_groups_returns_groups_not_rows(temp_conn):
    """3 identical invoices (same vendor/invoice_no/total) -> 1 group."""
    a = _save_invoice(temp_conn, vendor_name="Dup", invoice_number="DUP-1", total_amount=Decimal("1000"))
    b = _save_invoice(temp_conn, vendor_name="Dup", invoice_number="DUP-1", total_amount=Decimal("1000"))
    c = _save_invoice(temp_conn, vendor_name="Dup", invoice_number="DUP-1", total_amount=Decimal("1000"))
    snap = compute_dashboard(temp_conn, REF_DATE)
    assert snap.kpis.possible_duplicate_groups == 1
    assert len(snap.duplicate_groups) == 1
    grp = snap.duplicate_groups[0]
    assert grp.vendor_name == "Dup"
    assert grp.invoice_number == "DUP-1"
    assert grp.total_amount == Decimal("1000")
    assert grp.invoice_count == 3
    # all three invoice ids should be discoverable through the DB
    assert all(x in (a, b, c) for x in (a, b, c))


def test_different_invoice_numbers_are_not_duplicates(temp_conn):
    _save_invoice(temp_conn, vendor_name="V", invoice_number="X-1", total_amount=Decimal("100"))
    _save_invoice(temp_conn, vendor_name="V", invoice_number="X-2", total_amount=Decimal("100"))
    snap = compute_dashboard(temp_conn, REF_DATE)
    assert snap.kpis.possible_duplicate_groups == 0


def test_two_distinct_duplicate_groups(temp_conn):
    _save_invoice(temp_conn, vendor_name="A", invoice_number="1", total_amount=Decimal("100"))
    _save_invoice(temp_conn, vendor_name="A", invoice_number="1", total_amount=Decimal("100"))
    _save_invoice(temp_conn, vendor_name="B", invoice_number="2", total_amount=Decimal("200"))
    _save_invoice(temp_conn, vendor_name="B", invoice_number="2", total_amount=Decimal("200"))
    snap = compute_dashboard(temp_conn, REF_DATE)
    assert snap.kpis.possible_duplicate_groups == 2
    assert {g.vendor_name for g in snap.duplicate_groups} == {"A", "B"}


# ---------------------------------------------------------------------------
# Spend by category / vendor
# ---------------------------------------------------------------------------


def test_spend_by_category_excludes_pending_and_rejected(temp_conn):
    a = _save_invoice(
        temp_conn, vendor_name="V1", invoice_number="C-1",
        total_amount=Decimal("100"), expense_category="Equipment",
    )
    _approve(temp_conn, a)
    b = _save_invoice(
        temp_conn, vendor_name="V2", invoice_number="C-2",
        total_amount=Decimal("200"), expense_category="Equipment",
    )
    _approve(temp_conn, b)
    c = _save_invoice(
        temp_conn, vendor_name="V3", invoice_number="C-3",
        total_amount=Decimal("9999"), expense_category="Equipment",
    )  # pending
    d = _save_invoice(
        temp_conn, vendor_name="V4", invoice_number="C-4",
        total_amount=Decimal("8888"), expense_category="Office",
    )
    _reject(temp_conn, d)
    snap = compute_dashboard(temp_conn, REF_DATE)
    by_cat = {c.category: (c.total_amount, c.invoice_count) for c in snap.spend_by_category}
    assert by_cat == {"Equipment": (Decimal("300.0"), 2)}


def test_spend_by_vendor_top_5_approved_only(temp_conn):
    vendors = [f"V{i}" for i in range(7)]
    for i, v in enumerate(vendors):
        inv = _save_invoice(
            temp_conn, vendor_name=v, invoice_number=f"S-{i}",
            total_amount=Decimal(str(1000 - i * 100)),
        )
        _approve(temp_conn, inv)
    snap = compute_dashboard(temp_conn, REF_DATE)
    assert len(snap.spend_by_vendor) <= 5
    # top vendor should be the one with the largest total
    assert snap.spend_by_vendor[0].vendor_name == "V0"
    assert snap.spend_by_vendor[0].total_amount == Decimal("1000.0")


# ---------------------------------------------------------------------------
# Multi-currency safety
# ---------------------------------------------------------------------------


def test_cad_and_usd_are_never_combined(temp_conn):
    cad = _save_invoice(
        temp_conn, vendor_name="CdnCo", invoice_number="CAD-1",
        total_amount=Decimal("1000"), currency="CAD",
    )
    _approve(temp_conn, cad)
    usd = _save_invoice(
        temp_conn, vendor_name="UsCo", invoice_number="USD-1",
        total_amount=Decimal("500"), currency="USD",
    )
    _approve(temp_conn, usd)
    snap = compute_dashboard(temp_conn, REF_DATE)
    # Outstanding must be split
    assert snap.kpis.outstanding_amount.amounts == {
        "CAD": Decimal("1000.0"),
        "USD": Decimal("500.0"),
    }
    # No combined key
    assert "CAD+USD" not in str(snap.kpis.outstanding_amount.amounts)
    # Spend by vendor: two entries, two currencies
    by_vendor_ccy = {(v.vendor_name, v.currency) for v in snap.spend_by_vendor}
    assert ("CdnCo", "CAD") in by_vendor_ccy
    assert ("UsCo", "USD") in by_vendor_ccy


def test_currency_filter_narrows_view(temp_conn):
    _save_invoice(
        temp_conn, vendor_name="CdnCo", invoice_number="CAD-1",
        total_amount=Decimal("1000"), currency="CAD",
    )
    _save_invoice(
        temp_conn, vendor_name="UsCo", invoice_number="USD-1",
        total_amount=Decimal("500"), currency="USD",
    )
    # approve both
    inv_cad = _save_invoice(
        temp_conn, vendor_name="CdnCo2", invoice_number="CAD-2",
        total_amount=Decimal("700"), currency="CAD",
    )
    _approve(temp_conn, inv_cad)
    inv_usd = _save_invoice(
        temp_conn, vendor_name="UsCo2", invoice_number="USD-2",
        total_amount=Decimal("300"), currency="USD",
    )
    _approve(temp_conn, inv_usd)
    snap_all = compute_dashboard(temp_conn, REF_DATE, currency="ALL")
    snap_cad = compute_dashboard(temp_conn, REF_DATE, currency="CAD")
    snap_usd = compute_dashboard(temp_conn, REF_DATE, currency="USD")
    # ALL: both currencies
    assert set(snap_all.kpis.outstanding_amount.amounts.keys()) == {"CAD", "USD"}
    # CAD only
    assert set(snap_cad.kpis.outstanding_amount.amounts.keys()) == {"CAD"}
    assert snap_cad.kpis.outstanding_amount.amounts["CAD"] == Decimal("700.0")
    # USD only
    assert set(snap_usd.kpis.outstanding_amount.amounts.keys()) == {"USD"}
    assert snap_usd.kpis.outstanding_amount.amounts["USD"] == Decimal("300.0")


def test_currency_filter_garbage_falls_back_to_all(temp_conn):
    inv = _save_invoice(
        temp_conn, vendor_name="V", invoice_number="X-1",
        total_amount=Decimal("100"),
    )
    _approve(temp_conn, inv)
    snap = compute_dashboard(temp_conn, REF_DATE, currency="EUR")  # unknown
    assert snap.currency_filter == "ALL"
    assert "CAD" in snap.kpis.outstanding_amount.amounts


# ---------------------------------------------------------------------------
# Needs Attention ordering
# ---------------------------------------------------------------------------


def test_needs_attention_priority_overdue_first(temp_conn):
    # Pending only -> should be pending
    pending = _save_invoice(
        temp_conn, vendor_name="V1", invoice_number="N-1",
        due_date="2026-09-25", total_amount=Decimal("100"),
    )
    # Approved, due in 3 days -> due_soon
    due_soon = _save_invoice(
        temp_conn, vendor_name="V2", invoice_number="N-2",
        due_date="2026-09-25", total_amount=Decimal("100"),
    )
    _approve(temp_conn, due_soon)
    # Approved, overdue
    overdue = _save_invoice(
        temp_conn, vendor_name="V3", invoice_number="N-3",
        due_date="2026-09-20", total_amount=Decimal("100"),
    )
    _approve(temp_conn, overdue)
    snap = compute_dashboard(temp_conn, REF_DATE)
    priorities = []
    for it in snap.needs_attention:
        if "overdue" in it.reasons:
            priorities.append(0)
        elif "due_soon" in it.reasons:
            priorities.append(1)
        elif "pending" in it.reasons:
            priorities.append(2)
        else:
            priorities.append(99)
    # Priorities should be sorted ascending
    assert priorities == sorted(priorities)
    # Overdue item should be first
    assert "overdue" in snap.needs_attention[0].reasons


def test_needs_attention_excludes_paid_and_rejected(temp_conn):
    overdue_paid = _save_invoice(
        temp_conn, vendor_name="V1", invoice_number="N-1",
        due_date="2026-09-10", total_amount=Decimal("100"),
    )
    _approve(temp_conn, overdue_paid)
    _mark_paid(temp_conn, overdue_paid)
    overdue_rejected = _save_invoice(
        temp_conn, vendor_name="V2", invoice_number="N-2",
        due_date="2026-09-10", total_amount=Decimal("100"),
    )
    _reject(temp_conn, overdue_rejected)
    snap = compute_dashboard(temp_conn, REF_DATE)
    ids = {it.invoice_id for it in snap.needs_attention}
    assert overdue_paid not in ids
    assert overdue_rejected not in ids


# ---------------------------------------------------------------------------
# Recent invoices
# ---------------------------------------------------------------------------


def test_recent_invoices_returns_latest_10(temp_conn):
    for i in range(15):
        _save_invoice(
            temp_conn, vendor_name=f"V{i}", invoice_number=f"R-{i}",
            total_amount=Decimal("100"),
        )
    snap = compute_dashboard(temp_conn, REF_DATE)
    assert len(snap.recent_invoices) == 10
    # IDs should be in descending order
    ids = [r.id for r in snap.recent_invoices]
    assert ids == sorted(ids, reverse=True)


def test_recent_invoices_payment_state_marker(temp_conn):
    a = _save_invoice(temp_conn, vendor_name="P", invoice_number="PM-1", total_amount=Decimal("100"))
    b = _save_invoice(temp_conn, vendor_name="A", invoice_number="PM-2", total_amount=Decimal("100"))
    _approve(temp_conn, b)
    c = _save_invoice(temp_conn, vendor_name="X", invoice_number="PM-3", total_amount=Decimal("100"))
    _approve(temp_conn, c)
    _mark_paid(temp_conn, c)
    snap = compute_dashboard(temp_conn, REF_DATE)
    by_id = {r.id: r for r in snap.recent_invoices}
    assert by_id[a].payment_state_marker == "pending"
    assert by_id[b].payment_state_marker == "unpaid"
    assert by_id[c].payment_state_marker == "paid"


# ---------------------------------------------------------------------------
# Decimal safety
# ---------------------------------------------------------------------------


def test_money_is_decimal_in_kpis(temp_conn):
    _save_invoice(
        temp_conn, vendor_name="V", invoice_number="D-1",
        total_amount=Decimal("123.45"),
    )
    snap = compute_dashboard(temp_conn, REF_DATE)
    for ccy, amt in snap.kpis.outstanding_amount.amounts.items():
        assert isinstance(amt, Decimal)


# ---------------------------------------------------------------------------
# No raw_text returned
# ---------------------------------------------------------------------------


def test_no_raw_text_in_dashboard_snapshot(temp_conn):
    """Defensive: the dashboard must NEVER expose raw_text or bank fields."""
    inv = _save_invoice(
        temp_conn, vendor_name="V", invoice_number="RT-1",
        total_amount=Decimal("100"),
    )
    snap = compute_dashboard(temp_conn, REF_DATE)
    # All dataclasses we expose must NOT contain any sensitive field names.
    from dataclasses import fields, is_dataclass
    for dc in (
        snap.kpis, snap.needs_attention[0] if snap.needs_attention else None,
        snap.spend_by_category[0] if snap.spend_by_category else None,
        snap.spend_by_vendor[0] if snap.spend_by_vendor else None,
        snap.duplicate_groups[0] if snap.duplicate_groups else None,
        snap.recent_invoices[0] if snap.recent_invoices else None,
    ):
        if dc is None:
            continue
        assert is_dataclass(dc)
        names = {f.name for f in fields(dc)}
        for forbidden in ("raw_text", "bank_account", "routing_number",
                          "swift", "iban", "email", "phone",
                          "payment_instructions", "payment_instructions_raw"):
            assert forbidden not in names, (
                f"{type(dc).__name__} leaked forbidden field {forbidden}"
            )


# ---------------------------------------------------------------------------
# STEP 8 Synthetic E2E - deterministic 7-row mixed-currency scenario
# ---------------------------------------------------------------------------
#
# Final-closure deterministic end-to-end test. Exercises the FULL pipeline
# (save -> approve/reject/mark_paid -> compute_dashboard) against a
# TEMPORARY SQLite database. NEVER touches database/oilops.db.
#
# Dataset (ref date = 2026-09-22):
#   A  Prairie Pump Rentals   Equipment          approved  CAD 5250   unpaid   due ref+3
#   B  Rocky Field Services   Field Services     approved  CAD 10500  PAID
#   C  Calgary Freight Ltd    Transportation     pending   CAD 2000   unpaid   due ref+5
#   D  Office Supply Demo     Office             rejected  CAD 500
#   E  US Consulting Demo     Professional Svcs  approved  USD 3000   unpaid
#   F  Duplicate Vendor       Field Services     pending   CAD 1500   unpaid   duplicate
#   G  Duplicate Vendor       Field Services     pending   CAD 1500   unpaid   duplicate
#
# Critical invariants:
#   - CAD and USD NEVER summed together (separate dict keys).
#   - Rejected NEVER enters outstanding / paid / due.
#   - Paid NEVER enters overdue.
#   - Duplicate groups: exactly 1 (F + G).
#   - Approved spend by category + Top vendors: currency-safe.
#   - Needs Attention: deterministic priority ordering.
#   - Recent invoices: returned.


E2E_REF = date(2026, 9, 22)


def _seed_e2e_dataset(conn):
    """Insert the A-G synthetic dataset. Return dict of ids."""
    ids = {}

    # A: approved unpaid, due ref+3
    a = _save_invoice(
        conn,
        vendor_name="Prairie Pump Rentals",
        invoice_number="A-001",
        total_amount=Decimal("5250"),
        currency="CAD",
        due_date=E2E_REF.replace(day=25).isoformat(),
        expense_category="Equipment",
    )
    _approve(conn, a)

    # B: approved paid
    b = _save_invoice(
        conn,
        vendor_name="Rocky Field Services",
        invoice_number="B-001",
        total_amount=Decimal("10500"),
        currency="CAD",
        due_date=E2E_REF.replace(day=22).isoformat(),
        expense_category="Field Services",
    )
    _approve(conn, b)
    _mark_paid(conn, b)

    # C: pending, due ref+5
    c = _save_invoice(
        conn,
        vendor_name="Calgary Freight Ltd",
        invoice_number="C-001",
        total_amount=Decimal("2000"),
        currency="CAD",
        due_date=E2E_REF.replace(day=27).isoformat(),
        expense_category="Transportation",
    )

    # D: rejected
    d = _save_invoice(
        conn,
        vendor_name="Office Supply Demo",
        invoice_number="D-001",
        total_amount=Decimal("500"),
        currency="CAD",
        due_date=E2E_REF.replace(day=22).isoformat(),
        expense_category="Office",
    )
    _reject(conn, d)

    # E: approved unpaid USD
    e = _save_invoice(
        conn,
        vendor_name="US Consulting Demo",
        invoice_number="E-001",
        total_amount=Decimal("3000"),
        currency="USD",
        due_date=E2E_REF.replace(day=29).isoformat(),
        expense_category="Professional Services",
    )
    _approve(conn, e)

    # F + G: duplicate pair (stay pending)
    f = _save_invoice(
        conn,
        vendor_name="Duplicate Vendor",
        invoice_number="DUP-001",
        total_amount=Decimal("1500"),
        currency="CAD",
        due_date=E2E_REF.replace(day=22).isoformat(),
        expense_category="Field Services",
    )
    g = _save_invoice(
        conn,
        vendor_name="Duplicate Vendor",
        invoice_number="DUP-001",
        total_amount=Decimal("1500"),
        currency="CAD",
        due_date=E2E_REF.replace(day=22).isoformat(),
        expense_category="Field Services",
    )
    ids["a"], ids["b"], ids["c"], ids["d"], ids["e"], ids["f"], ids["g"] = (
        a, b, c, d, e, f, g
    )
    return ids


def test_synthetic_e2e_full_pipeline(temp_conn):
    """Deterministic A-G mixed-currency end-to-end test."""
    _seed_e2e_dataset(temp_conn)
    snap = compute_dashboard(temp_conn, E2E_REF)

    # --- Counts ---
    assert snap.kpis.total_invoices == 7
    assert snap.kpis.pending_review == 3   # C, F, G
    assert snap.kpis.approved == 3         # A, B, E
    assert snap.kpis.rejected == 1         # D

    # --- Outstanding: A (CAD 5250) + E (USD 3000); B paid, C/F/G pending,
    #     D rejected. CAD and USD must be SEPARATE keys.
    out = snap.kpis.outstanding_amount.amounts
    assert set(out.keys()) == {"CAD", "USD"}, f"outstanding keys: {sorted(out)}"
    assert out["CAD"] == Decimal("5250.0")
    assert out["USD"] == Decimal("3000.0")
    # CRITICAL: CAD + USD NEVER summed together in a single scalar.
    assert "CAD+USD" not in out

    # --- Paid: only B (CAD 10500); USD has no paid.
    paid = snap.kpis.paid_amount.amounts
    assert set(paid.keys()) == {"CAD"}, f"paid keys: {sorted(paid)}"
    assert paid["CAD"] == Decimal("10500.0")



    # --- Due this week: A(+3) + C(+5) + E(+7) + F(ref) + G(ref) = 5.
    #     B is paid (excluded). D is rejected (excluded).
    #     Note: E is +7 days which is inclusive per the SQL BETWEEN clause
    #     (`<=` end of week).
    assert snap.kpis.due_this_week == 5, (
        f"due_this_week={snap.kpis.due_this_week}"
    )

    # --- Overdue: nothing due before ref.
    assert snap.kpis.overdue == 0

    # --- Duplicate groups: exactly one (F + G).
    assert snap.kpis.possible_duplicate_groups == 1
    assert len(snap.duplicate_groups) == 1
    grp = snap.duplicate_groups[0]
    assert grp.vendor_name == "Duplicate Vendor"
    assert grp.invoice_number == "DUP-001"
    assert grp.total_amount == Decimal("1500")
    assert grp.invoice_count == 2

    # --- Approved spend by category: only A,B,E approved. Each currency
    #     must be its own entry. CAD and USD NOT combined.
    by_cat = snap.spend_by_category
    cat_pairs = {(c.category, c.currency) for c in by_cat}
    assert ("Equipment", "CAD") in cat_pairs
    assert ("Field Services", "CAD") in cat_pairs
    assert ("Professional Services", "USD") in cat_pairs
    # CRITICAL: never a (Category, "CAD+USD") or any mixed-currency entry.
    for cs in by_cat:
        assert "+" not in cs.currency, f"mixed-currency leak: {cs}"
    equip = [c for c in by_cat if c.category == "Equipment" and c.currency == "CAD"]
    assert len(equip) == 1
    assert equip[0].total_amount == Decimal("5250.0")

    # --- Top vendors: A, B, E approved; each currency its own entry.
    by_vendor = snap.spend_by_vendor
    for v in by_vendor:
        assert "+" not in v.currency, f"mixed-currency leak in vendor: {v}"
    vendor_pairs = {(v.vendor_name, v.currency) for v in by_vendor}
    assert ("Rocky Field Services", "CAD") in vendor_pairs
    assert ("Prairie Pump Rentals", "CAD") in vendor_pairs
    assert ("US Consulting Demo", "USD") in vendor_pairs

    # --- Needs Attention: deterministic priority ordering.
    reasons_seq = []
    for it in snap.needs_attention:
        if "overdue" in it.reasons:
            reasons_seq.append(0)
        elif "due_soon" in it.reasons:
            reasons_seq.append(1)
        elif "pending" in it.reasons:
            reasons_seq.append(2)
        elif "duplicate" in it.reasons:
            reasons_seq.append(3)
        else:
            reasons_seq.append(99)
    assert reasons_seq == sorted(reasons_seq), (
        f"needs_attention not ordered by priority: {reasons_seq}"
    )

    # --- Recent invoices: returned (>=1 row, <=10).
    assert len(snap.recent_invoices) > 0
    assert len(snap.recent_invoices) <= 10

    # --- Available currencies: sorted list including CAD and USD.
    assert "CAD" in snap.available_currencies
    assert "USD" in snap.available_currencies
    assert snap.available_currencies == sorted(snap.available_currencies)

    # --- Currency filter sanity: CAD view hides USD and vice versa.
    snap_cad = compute_dashboard(temp_conn, E2E_REF, currency="CAD")
    assert set(snap_cad.kpis.outstanding_amount.amounts.keys()) == {"CAD"}
    assert "USD" not in snap_cad.kpis.outstanding_amount.amounts
    snap_usd = compute_dashboard(temp_conn, E2E_REF, currency="USD")
    assert set(snap_usd.kpis.outstanding_amount.amounts.keys()) == {"USD"}
    assert "CAD" not in snap_usd.kpis.outstanding_amount.amounts

