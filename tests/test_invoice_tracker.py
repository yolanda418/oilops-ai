"""Batch 3 - Practical Invoice Tracker tests.

Covers the Batch 3 acceptance list:
- vendor search
- invoice # search
- PO search
- filters (review, payment, category, currency, due-state)
- invoice detail
- legacy invoice without PDF
- edit pending invoice + revalidate warning update
- disallow edit on approved / rejected invoice
- existing approval / payment still works (regression smoke)

Design rules honoured by the tests:
* raw_text is never persisted (assertion in edit test).
* Original PDFs are NEVER passed to the LLM (no AI call is made).
* Edit / search SQL is parameterised.
"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest

from database.db import get_connection, init_db
from services.ai_classifier import ClassificationResult, SOURCE_MOCK
from services.invoice_checker import (
    CODE_AMOUNT_MISMATCH,
    CODE_MISSING_INVOICE_NUMBER,
    CODE_MISSING_PO,
    InvoiceValidationResult,
    ValidationIssue,
    SEVERITY_WARNING,
    SEVERITY_ERROR,
    SEVERITY_INFO,
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
    update_document_ref,
)
from services.invoice_tracker import (
    ALL_DUE_STATES,
    ALL_PAYMENT_FILTERS,
    ALL_REVIEW_FILTERS,
    CATEGORY_FILTER_ALL,
    DUE_STATE_DUE_LATER,
    DUE_STATE_DUE_SOON,
    DUE_STATE_NO_DUE_DATE,
    DUE_STATE_OVERDUE,
    EDITABLE_FIELDS,
    InvalidAmountError,
    InvalidEditFieldError,
    InvalidFilterValueError,
    InvoiceNotEditableError,
    PAYMENT_FILTER_OUTSTANDING,
    PAYMENT_FILTER_PAID,
    REVIEW_FILTER_APPROVED,
    REVIEW_FILTER_PENDING,
    PAYMENT_FILTER_ALL,
    REVIEW_FILTER_ALL,
    REVIEW_FILTER_REJECTED,
    search_tracker,
    get_tracker_detail,
    update_pending_invoice_fields,
)

@pytest.fixture()
def temp_db_path(tmp_path, monkeypatch):
    db_path = tmp_path / "batch3_tracker.db"
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


def _make_validation(*issues):
    return InvoiceValidationResult(issues=list(issues))


def _save(temp_conn, **overrides):
    category = overrides.pop("expense_category", "Equipment")
    source = overrides.pop("source_filename", "x.pdf")
    ext = _make_extraction(**overrides)
    return save_pending(
        temp_conn, ext,
        source_filename=source,
        expense_category=category,
        classification_source="mock",
        validation=_make_validation(),
        reviewer="Alice",
    )


def test_search_tracker_empty_returns_empty_list(temp_conn):
    rows = search_tracker(temp_conn)
    assert rows == []


def test_search_by_vendor(temp_conn):
    _save(temp_conn, vendor_name="Prairie Pump Rentals",
          invoice_number="PPR-1", total_amount=Decimal("100.00"))
    _save(temp_conn, vendor_name="Northern Lights Welding",
          invoice_number="NLW-9", total_amount=Decimal("200.00"))
    rows = search_tracker(temp_conn, search="Prairie")
    assert len(rows) == 1
    assert rows[0].vendor_name == "Prairie Pump Rentals"


def test_search_by_invoice_number(temp_conn):
    _save(temp_conn, vendor_name="Prairie Pump Rentals",
          invoice_number="PPR-2026-100", total_amount=Decimal("100.00"))
    _save(temp_conn, vendor_name="Northern Lights Welding",
          invoice_number="NLW-9", total_amount=Decimal("200.00"))
    rows = search_tracker(temp_conn, search="NLW-9")
    assert len(rows) == 1
    assert rows[0].invoice_number == "NLW-9"


def test_search_by_po_number(temp_conn):
    _save(temp_conn, vendor_name="Prairie Pump Rentals",
          invoice_number="PPR-1", po_number="PO-AAA", total_amount=Decimal("100.00"))
    _save(temp_conn, vendor_name="Northern Lights Welding",
          invoice_number="NLW-1", po_number="PO-BBB", total_amount=Decimal("200.00"))
    rows = search_tracker(temp_conn, search="PO-AAA")
    assert len(rows) == 1
    assert rows[0].po_number == "PO-AAA"

def test_filter_by_review_status(temp_conn):
    a = _save(temp_conn, invoice_number="PPR-1", total_amount=Decimal("100.00"))
    b = _save(temp_conn, invoice_number="PPR-2", total_amount=Decimal("200.00"))
    c = _save(temp_conn, invoice_number="PPR-3", total_amount=Decimal("300.00"))
    approve_invoice(temp_conn, b, reviewer="Alice")
    reject_invoice(temp_conn, c, reviewer="Alice", review_note="No PO")

    rows_p = search_tracker(temp_conn, review_filter=REVIEW_FILTER_PENDING)
    rows_a = search_tracker(temp_conn, review_filter=REVIEW_FILTER_APPROVED)
    rows_r = search_tracker(temp_conn, review_filter=REVIEW_FILTER_REJECTED)
    assert {r.id for r in rows_p} == {a}
    assert {r.id for r in rows_a} == {b}
    assert {r.id for r in rows_r} == {c}


def test_filter_by_payment_status(temp_conn):
    a = _save(temp_conn, invoice_number="PPR-1", total_amount=Decimal("100.00"))
    b = _save(temp_conn, invoice_number="PPR-2", total_amount=Decimal("200.00"))
    c = _save(temp_conn, invoice_number="PPR-3", total_amount=Decimal("300.00"))
    approve_invoice(temp_conn, b, reviewer="Alice")
    approve_invoice(temp_conn, c, reviewer="Alice")
    mark_paid(temp_conn, c, paid_at="2026-10-01T10:00:00")

    outstanding = search_tracker(
        temp_conn, payment_filter=PAYMENT_FILTER_OUTSTANDING
    )
    paid = search_tracker(temp_conn, payment_filter=PAYMENT_FILTER_PAID)
    assert {r.id for r in outstanding} == {b}
    assert {r.id for r in paid} == {c}
    # pending (not approved) should not appear under payment filter
    assert a not in {r.id for r in outstanding}
    assert a not in {r.id for r in paid}


def test_filter_by_category_and_currency(temp_conn):
    _save(temp_conn, invoice_number="PPR-1", total_amount=Decimal("100.00"),
          expense_category="Equipment", currency="CAD")
    _save(temp_conn, invoice_number="PPR-2", total_amount=Decimal("200.00"),
          expense_category="Fuel", currency="USD")

    rows_equipment = search_tracker(temp_conn, category="Equipment")
    rows_fuel = search_tracker(temp_conn, category="Fuel")
    assert len(rows_equipment) == 1
    assert rows_equipment[0].expense_category == "Equipment"
    assert len(rows_fuel) == 1
    assert rows_fuel[0].currency == "USD"


def test_filter_by_due_state(temp_conn):
    # overdue: due date in the past
    _save(temp_conn, invoice_number="PPR-1", total_amount=Decimal("100.00"),
          due_date="2020-01-01")
    # due soon: due date within 7 days of reference
    _save(temp_conn, invoice_number="PPR-2", total_amount=Decimal("200.00"),
          due_date="2026-09-29")
    # due later: due date >7 days out
    _save(temp_conn, invoice_number="PPR-3", total_amount=Decimal("300.00"),
          due_date="2027-01-01")
    # no due date
    _save(temp_conn, invoice_number="PPR-4", total_amount=Decimal("400.00"),
          due_date=None)

    ref = date(2026, 9, 26)
    overdue = search_tracker(temp_conn, due_state=DUE_STATE_OVERDUE,
                             reference_date=ref)
    soon = search_tracker(temp_conn, due_state=DUE_STATE_DUE_SOON,
                          reference_date=ref)
    later = search_tracker(temp_conn, due_state=DUE_STATE_DUE_LATER,
                           reference_date=ref)
    nodue = search_tracker(temp_conn, due_state=DUE_STATE_NO_DUE_DATE,
                           reference_date=ref)

    assert {r.id for r in overdue} == {1}
    assert {r.id for r in soon} == {2}
    assert {r.id for r in later} == {3}
    assert {r.id for r in nodue} == {4}


def test_combined_filters(temp_conn):
    _save(temp_conn, invoice_number="PPR-1", total_amount=Decimal("100.00"),
          due_date="2020-01-01", currency="CAD")
    _save(temp_conn, invoice_number="PPR-2", total_amount=Decimal("200.00"),
          due_date="2027-01-01", currency="USD")
    rows = search_tracker(
        temp_conn,
        search="PPR",
        currency="USD",
        due_state=DUE_STATE_DUE_LATER,
        reference_date=date(2026, 9, 26),
    )
    assert len(rows) == 1
    assert rows[0].currency == "USD"


def test_invalid_review_filter_raises(temp_conn):
    with pytest.raises(InvalidFilterValueError):
        search_tracker(temp_conn, review_filter="Bogus")


def test_invalid_currency_filter_raises(temp_conn):
    with pytest.raises(InvalidFilterValueError):
        search_tracker(temp_conn, currency="EUR")

def test_get_tracker_detail_includes_all_fields(temp_conn):
    new_id = _save(temp_conn, invoice_number="PPR-DETAIL",
                   total_amount=Decimal("5250.00"),
                   currency="CAD", due_date="2026-09-30")
    detail = get_tracker_detail(temp_conn, new_id)
    inv = detail.invoice
    assert inv.vendor_name == "Prairie Pump Rentals"
    assert inv.invoice_number == "PPR-DETAIL"
    assert inv.invoice_date == "2026-09-15"
    assert inv.due_date == "2026-09-30"
    assert inv.po_number == "PO-1042"
    assert inv.expense_category == "Equipment"
    assert inv.subtotal == pytest.approx(5000.0, rel=0.001)
    assert inv.gst == pytest.approx(250.0, rel=0.001)
    assert inv.total_amount == pytest.approx(5250.0, rel=0.001)
    assert inv.currency == "CAD"
    assert inv.status == STATUS_PENDING
    if detail.validation is not None:
        assert not detail.validation.has_issues


def test_detail_legacy_row_without_pdf_does_not_explode(temp_conn):
    cur = temp_conn.execute(
        "INSERT INTO invoices (vendor_name, total_amount, status) "
        "VALUES (?, ?, ?)",
        ("Legacy Vendor", 100.0, STATUS_APPROVED),
    )
    legacy_id = int(cur.lastrowid)
    temp_conn.commit()
    detail = get_tracker_detail(temp_conn, legacy_id)
    assert detail.invoice.id == legacy_id
    assert detail.invoice.document_path is None
    assert detail.invoice.document_sha256 is None
    assert detail.invoice.document_size_bytes is None


def test_detail_decodes_stored_validation_flags(temp_conn):
    flags = json.dumps({
        "issue_count": 2,
        "error_count": 0,
        "warning_count": 1,
        "info_count": 1,
        "issues": [
            {
                "code": CODE_MISSING_PO,
                "severity": SEVERITY_WARNING,
                "message": "PO number is missing.",
                "field": "po_number",
            },
            {
                "code": "INFO_X",
                "severity": SEVERITY_INFO,
                "message": "info only",
                "field": None,
            },
        ],
    })
    cur = temp_conn.execute(
        "INSERT INTO invoices (vendor_name, total_amount, status, "
        "validation_flags) VALUES (?, ?, ?, ?)",
        ("Acme", 100.0, STATUS_PENDING, flags),
    )
    iid = int(cur.lastrowid)
    temp_conn.commit()
    detail = get_tracker_detail(temp_conn, iid)
    assert detail.validation is not None
    codes = [i.code for i in detail.validation.issues]
    assert CODE_MISSING_PO in codes
    assert "INFO_X" in codes

def test_edit_pending_updates_field_and_revalidates(temp_conn):
    new_id = _save(temp_conn, invoice_number="PPR-1",
                   total_amount=Decimal("5250.00"),
                   subtotal=Decimal("5000.00"), gst=Decimal("250.00"))
    detail = update_pending_invoice_fields(
        temp_conn, new_id,
        {"total_amount": "9999.00"},
        reference_date=date(2026, 9, 26),
    )
    assert detail.invoice.total_amount == pytest.approx(9999.0, rel=0.001)
    assert detail.validation is not None
    assert any(i.code == CODE_AMOUNT_MISMATCH for i in detail.validation.issues)


def test_edit_rejects_unknown_field(temp_conn):
    new_id = _save(temp_conn, invoice_number="PPR-2",
                   total_amount=Decimal("100.00"))
    with pytest.raises(InvalidEditFieldError):
        update_pending_invoice_fields(temp_conn, new_id, {"status": "approved"})


def test_edit_rejects_bad_amount(temp_conn):
    new_id = _save(temp_conn, invoice_number="PPR-3",
                   total_amount=Decimal("100.00"))
    with pytest.raises(InvalidAmountError):
        update_pending_invoice_fields(temp_conn, new_id, {"total_amount": "abc"})


def test_edit_rejects_bad_date(temp_conn):
    new_id = _save(temp_conn, invoice_number="PPR-4",
                   total_amount=Decimal("100.00"))
    with pytest.raises(InvalidAmountError):
        update_pending_invoice_fields(temp_conn, new_id, {"due_date": "not-a-date"})


def test_edit_rejects_bad_currency(temp_conn):
    new_id = _save(temp_conn, invoice_number="PPR-5",
                   total_amount=Decimal("100.00"))
    with pytest.raises(InvalidAmountError):
        update_pending_invoice_fields(temp_conn, new_id, {"currency": "EUR"})


def test_edit_disallowed_on_approved(temp_conn):
    new_id = _save(temp_conn, invoice_number="PPR-6",
                   total_amount=Decimal("100.00"))
    approve_invoice(temp_conn, new_id, reviewer="Alice")
    with pytest.raises(InvoiceNotEditableError):
        update_pending_invoice_fields(temp_conn, new_id, {"vendor_name": "X"})


def test_edit_disallowed_on_rejected(temp_conn):
    new_id = _save(temp_conn, invoice_number="PPR-7",
                   total_amount=Decimal("100.00"))
    reject_invoice(temp_conn, new_id, reviewer="Alice", review_note="no PO")
    with pytest.raises(InvoiceNotEditableError):
        update_pending_invoice_fields(temp_conn, new_id, {"vendor_name": "X"})


def test_edit_does_not_persist_raw_text(temp_conn):
    new_id = _save(temp_conn, invoice_number="PPR-8",
                   total_amount=Decimal("100.00"))
    before = temp_conn.execute(
        "SELECT raw_text FROM invoices WHERE id = ?", (new_id,)
    ).fetchone()["raw_text"]
    assert before is None
    update_pending_invoice_fields(
        temp_conn, new_id,
        {"vendor_name": "Brand New Vendor"},
        reference_date=date(2026, 9, 26),
    )
    after = temp_conn.execute(
        "SELECT raw_text FROM invoices WHERE id = ?", (new_id,)
    ).fetchone()["raw_text"]
    assert after is None


def test_edit_updates_validation_warnings_after_fix(temp_conn):
    # Initial state is internally consistent (100 + 10 = 110).
    new_id = _save(temp_conn, invoice_number="PPR-9",
                   subtotal=Decimal("100.00"), gst=Decimal("10.00"),
                   total_amount=Decimal("110.00"))
    # First edit introduces an amount mismatch (100 + 10 != 999).
    d1 = update_pending_invoice_fields(
        temp_conn, new_id,
        {"total_amount": "999.00"},
        reference_date=date(2026, 9, 26),
    )
    assert any(i.code == CODE_AMOUNT_MISMATCH for i in d1.validation.issues)
    # Second edit fixes the mismatch (100 + 10 == 110).
    d2 = update_pending_invoice_fields(
        temp_conn, new_id,
        {"total_amount": "110.00"},
        reference_date=date(2026, 9, 26),
    )
    assert not any(i.code == CODE_AMOUNT_MISMATCH for i in d2.validation.issues)


def test_approval_and_payment_still_work(temp_conn):
    new_id = _save(temp_conn, invoice_number="PPR-10",
                   total_amount=Decimal("100.00"))
    approved = approve_invoice(temp_conn, new_id, reviewer="Alice")
    assert approved.status == STATUS_APPROVED
    paid = mark_paid(
        temp_conn, new_id, paid_at="2026-10-01T10:00:00",
        payment_note="wired",
    )
    assert paid.status == STATUS_APPROVED
    assert paid.paid_at == "2026-10-01T10:00:00"
    assert paid.payment_state == "Paid"
    rows = search_tracker(temp_conn, payment_filter=PAYMENT_FILTER_PAID)
    assert {r.id for r in rows} == {new_id}


def test_editable_fields_constant_is_strict():
    forbidden = {
        "id", "status", "reviewer", "review_note", "paid_at", "payment_note",
        "raw_text", "extraction_json", "validation_flags",
        "document_path", "document_sha256", "document_size_bytes",
        "created_at", "updated_at", "source_filename",
        "expense_category", "classification_source",
    }
    assert forbidden.isdisjoint(set(EDITABLE_FIELDS))
    must_have = {
        "vendor_name", "invoice_number", "invoice_date", "due_date",
        "po_number", "subtotal", "gst", "total_amount", "currency",
        "description",
    }
    assert must_have.issubset(set(EDITABLE_FIELDS))


def test_filter_allow_lists_are_disjoint_and_complete():
    assert set(ALL_REVIEW_FILTERS) >= {REVIEW_FILTER_ALL, REVIEW_FILTER_PENDING,
                                       REVIEW_FILTER_APPROVED, REVIEW_FILTER_REJECTED}
    assert set(ALL_PAYMENT_FILTERS) >= {PAYMENT_FILTER_ALL,
                                        PAYMENT_FILTER_OUTSTANDING,
                                        PAYMENT_FILTER_PAID}
    assert set(ALL_DUE_STATES) >= {DUE_STATE_NO_DUE_DATE, DUE_STATE_OVERDUE,
                                   DUE_STATE_DUE_SOON, DUE_STATE_DUE_LATER}

