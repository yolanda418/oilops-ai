"""STEP 7 - invoice repository, approval, payment tracking tests."""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest

from database.db import get_connection, init_db
from services.ai_classifier import ClassificationResult, SOURCE_MOCK
from services.invoice_checker import (
    InvoiceValidationResult,
    ValidationIssue,
    SEVERITY_WARNING,
    CODE_AMOUNT_MISMATCH,
)
from services.invoice_extractor import InvoiceExtraction
from services.invoice_repository import (
    ALL_TRACKER_FILTERS,
    ALL_STATUSES,
    InvalidPaymentState,
    InvalidStateTransition,
    MissingReviewNoteError,
    MissingReviewerError,
    NotFoundError,
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_REJECTED,
    TRACKER_FILTER_ALL,
    TRACKER_FILTER_APPROVED,
    TRACKER_FILTER_OUTSTANDING,
    TRACKER_FILTER_PAID,
    TRACKER_FILTER_PENDING,
    TRACKER_FILTER_REJECTED,
    approve_invoice,
    get_invoice,
    list_payment_tracker,
    mark_paid,
    reject_invoice,
    save_pending,
    update_document_ref,
)


@pytest.fixture()
def temp_db_path(tmp_path, monkeypatch):
    db_path = tmp_path / "step7_repo.db"
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


def _make_classification(category="Equipment"):
    return ClassificationResult(
        category=category,
        confidence=0.85,
        reason="Matched keyword pump.",
        source=SOURCE_MOCK,
        used_provider="mock",
        is_low_confidence=False,
    )


# save_pending tests
def test_save_pending_creates_row_with_pending_status(temp_conn):
    ext = _make_extraction()
    new_id = save_pending(temp_conn, ext, source_filename="ppr.pdf",
                          expense_category="Equipment",
                          classification_source="mock",
                          classification=_make_classification("Equipment"),
                          validation=_make_validation(),
                          reviewer="Alice",
                          review_note=None)
    assert isinstance(new_id, int)
    row = temp_conn.execute(
        "SELECT status, vendor_name, total_amount, expense_category, "
        "classification_source, reviewer FROM invoices WHERE id = ?",
        (new_id,),
    ).fetchone()
    assert row["status"] == STATUS_PENDING
    assert row["vendor_name"] == "Prairie Pump Rentals"
    assert abs(row["total_amount"] - 5250.00) < 0.001
    assert row["expense_category"] == "Equipment"
    assert row["classification_source"] == "mock"
    assert row["reviewer"] == "Alice"


def test_save_pending_does_not_persist_raw_text(temp_conn):
    ext = _make_extraction()
    new_id = save_pending(temp_conn, ext, source_filename="ppr.pdf",
                          reviewer="Alice")
    row = temp_conn.execute(
        "SELECT raw_text FROM invoices WHERE id = ?", (new_id,)
    ).fetchone()
    assert row["raw_text"] is None


def test_save_pending_extraction_json_is_safe_allowlist(temp_conn):
    ext = _make_extraction()
    new_id = save_pending(temp_conn, ext, source_filename="ppr.pdf")
    row = temp_conn.execute(
        "SELECT extraction_json FROM invoices WHERE id = ?", (new_id,)
    ).fetchone()
    blob = row["extraction_json"]
    obj = json.loads(blob)
    allowed = {
        "vendor_name", "invoice_number", "invoice_date", "due_date",
        "po_number", "description", "subtotal", "gst",
        "total_amount", "currency",
    }
    assert set(obj.keys()) <= allowed
    for forbidden in (
        "raw_text", "bank_account", "routing_number", "swift",
        "iban", "email", "phone",
    ):
        assert forbidden not in obj, forbidden


def test_save_pending_validation_flags_is_safe_structure(temp_conn):
    validation = _make_validation(
        ValidationIssue(
            code=CODE_AMOUNT_MISMATCH,
            severity=SEVERITY_WARNING,
            message="Subtotal + tax != total.",
            field="total_amount",
        ),
    )
    ext = _make_extraction()
    new_id = save_pending(temp_conn, ext, source_filename="ppr.pdf",
                          validation=validation)
    row = temp_conn.execute(
        "SELECT validation_flags FROM invoices WHERE id = ?", (new_id,)
    ).fetchone()
    blob = row["validation_flags"]
    obj = json.loads(blob)
    assert obj["error_count"] == 0
    assert obj["warning_count"] == 1
    issue = obj["issues"][0]
    assert set(issue.keys()) <= {"code", "severity", "message", "field"}
    blob_str = json.dumps(obj)
    for forbidden in (
        "raw_text", "bank_account", "routing", "swift",
        "iban", "email", "phone", "Prairie", "5250",
    ):
        assert forbidden not in blob_str, forbidden


def test_save_pending_persists_expense_category(temp_conn):
    ext = _make_extraction()
    new_id = save_pending(temp_conn, ext, source_filename="ppr.pdf",
                          expense_category="Equipment")
    row = temp_conn.execute(
        "SELECT expense_category FROM invoices WHERE id = ?", (new_id,)
    ).fetchone()
    assert row["expense_category"] == "Equipment"


def test_save_pending_persists_classification_source(temp_conn):
    ext = _make_extraction()
    new_id = save_pending(temp_conn, ext, source_filename="ppr.pdf",
                          classification_source=SOURCE_MOCK,
                          classification=_make_classification("Equipment"))
    row = temp_conn.execute(
        "SELECT classification_source FROM invoices WHERE id = ?",
        (new_id,),
    ).fetchone()
    assert row["classification_source"] == SOURCE_MOCK


def test_save_pending_rejects_sensitive_injection(temp_conn):
    ext = _make_extraction()
    setattr(ext, "bank_account", "123-456-789")
    setattr(ext, "raw_text", "PRIVATE TEXT")
    new_id = save_pending(temp_conn, ext, source_filename="ppr.pdf")
    row = temp_conn.execute(
        "SELECT raw_text, extraction_json FROM invoices WHERE id = ?",
        (new_id,),
    ).fetchone()
    assert row["raw_text"] is None
    blob = row["extraction_json"]
    assert "123-456-789" not in blob
    assert "PRIVATE TEXT" not in blob
    assert "bank_account" not in blob


# approval state machine
def test_pending_can_be_approved(temp_conn):
    ext = _make_extraction()
    new_id = save_pending(temp_conn, ext, source_filename="ppr.pdf")
    saved = approve_invoice(temp_conn, new_id, reviewer="Bob",
                            review_note=None)
    assert saved.status == STATUS_APPROVED
    assert saved.reviewer == "Bob"
    assert saved.payment_state == "Outstanding"


def test_pending_can_be_rejected(temp_conn):
    ext = _make_extraction()
    new_id = save_pending(temp_conn, ext, source_filename="ppr.pdf")
    saved = reject_invoice(temp_conn, new_id, reviewer="Bob",
                           review_note="Wrong vendor.")
    assert saved.status == STATUS_REJECTED
    assert saved.reviewer == "Bob"
    assert saved.review_note == "Wrong vendor."
    assert saved.payment_state == "Not payable"


def test_approve_without_reviewer_raises(temp_conn):
    ext = _make_extraction()
    new_id = save_pending(temp_conn, ext, source_filename="ppr.pdf")
    with pytest.raises(MissingReviewerError):
        approve_invoice(temp_conn, new_id, reviewer="", review_note=None)
    with pytest.raises(MissingReviewerError):
        approve_invoice(temp_conn, new_id, reviewer=None, review_note=None)


def test_reject_without_reviewer_or_note_raises(temp_conn):
    ext = _make_extraction()
    new_id = save_pending(temp_conn, ext, source_filename="ppr.pdf")
    with pytest.raises(MissingReviewerError):
        reject_invoice(temp_conn, new_id, reviewer="",
                       review_note="bad invoice")
    with pytest.raises(MissingReviewNoteError):
        reject_invoice(temp_conn, new_id, reviewer="Bob", review_note="")
    with pytest.raises(MissingReviewNoteError):
        reject_invoice(temp_conn, new_id, reviewer="Bob", review_note=None)


def test_approve_twice_raises(temp_conn):
    ext = _make_extraction()
    new_id = save_pending(temp_conn, ext, source_filename="ppr.pdf")
    approve_invoice(temp_conn, new_id, reviewer="Bob")
    with pytest.raises(InvalidStateTransition):
        approve_invoice(temp_conn, new_id, reviewer="Carol")


def test_approve_after_reject_raises(temp_conn):
    ext = _make_extraction()
    new_id = save_pending(temp_conn, ext, source_filename="ppr.pdf")
    reject_invoice(temp_conn, new_id, reviewer="Bob", review_note="wrong")
    with pytest.raises(InvalidStateTransition):
        approve_invoice(temp_conn, new_id, reviewer="Carol")


def test_reject_after_approve_raises(temp_conn):
    ext = _make_extraction()
    new_id = save_pending(temp_conn, ext, source_filename="ppr.pdf")
    approve_invoice(temp_conn, new_id, reviewer="Bob")
    with pytest.raises(InvalidStateTransition):
        reject_invoice(temp_conn, new_id, reviewer="Carol",
                       review_note="late")


def test_get_invoice_unknown_raises(temp_conn):
    with pytest.raises(NotFoundError):
        get_invoice(temp_conn, 99999999)


# payment tracking
def test_pending_cannot_mark_paid(temp_conn):
    ext = _make_extraction()
    new_id = save_pending(temp_conn, ext, source_filename="ppr.pdf")
    with pytest.raises(InvalidPaymentState):
        mark_paid(temp_conn, new_id, paid_at="2026-09-22")


def test_rejected_cannot_mark_paid(temp_conn):
    ext = _make_extraction()
    new_id = save_pending(temp_conn, ext, source_filename="ppr.pdf")
    reject_invoice(temp_conn, new_id, reviewer="Bob", review_note="x")
    with pytest.raises(InvalidPaymentState):
        mark_paid(temp_conn, new_id, paid_at="2026-09-22")


def test_approved_can_mark_paid(temp_conn):
    ext = _make_extraction()
    new_id = save_pending(temp_conn, ext, source_filename="ppr.pdf")
    approve_invoice(temp_conn, new_id, reviewer="Bob")
    saved = mark_paid(temp_conn, new_id,
                      paid_at="2026-09-22",
                      payment_note="Wire ref ABC-001")
    assert saved.status == STATUS_APPROVED
    assert saved.paid_at == "2026-09-22"
    assert saved.payment_note == "Wire ref ABC-001"
    assert saved.payment_state == "Paid"


def test_cannot_mark_paid_twice(temp_conn):
    ext = _make_extraction()
    new_id = save_pending(temp_conn, ext, source_filename="ppr.pdf")
    approve_invoice(temp_conn, new_id, reviewer="Bob")
    mark_paid(temp_conn, new_id, paid_at="2026-09-22",
              payment_note="first")
    with pytest.raises(InvalidPaymentState):
        mark_paid(temp_conn, new_id, paid_at="2026-09-23",
                  payment_note="second")


def test_mark_paid_accepts_date_object(temp_conn):
    ext = _make_extraction()
    new_id = save_pending(temp_conn, ext, source_filename="ppr.pdf")
    approve_invoice(temp_conn, new_id, reviewer="Bob")
    saved = mark_paid(temp_conn, new_id, paid_at=date(2026, 9, 22),
                      payment_note=None)
    assert saved.paid_at == "2026-09-22"
    assert saved.payment_note is None
    assert saved.payment_state == "Paid"


def test_payment_columns_exist(temp_conn):
    cols = [r[1] for r in
            temp_conn.execute("PRAGMA table_info(invoices)").fetchall()]
    assert "paid_at" in cols
    assert "payment_note" in cols


def test_payment_state_derived_for_each_status(temp_conn):
    ext = _make_extraction()
    pid = save_pending(temp_conn, ext, source_filename="p.pdf")
    assert get_invoice(temp_conn, pid).payment_state == "Not ready"
    approve_invoice(temp_conn, pid, reviewer="Bob")
    assert get_invoice(temp_conn, pid).payment_state == "Outstanding"
    mark_paid(temp_conn, pid, paid_at="2026-09-22")
    assert get_invoice(temp_conn, pid).payment_state == "Paid"
    rid = save_pending(temp_conn, ext, source_filename="r.pdf")
    reject_invoice(temp_conn, rid, reviewer="Bob", review_note="x")
    assert get_invoice(temp_conn, rid).payment_state == "Not payable"


# tracker / filters
def _seed_three(conn):
    e1 = _make_extraction(vendor_name="V1", invoice_number="INV-1",
                          total_amount=Decimal("100.00"))
    e2 = _make_extraction(vendor_name="V2", invoice_number="INV-2",
                          total_amount=Decimal("200.00"))
    e3 = _make_extraction(vendor_name="V3", invoice_number="INV-3",
                          total_amount=Decimal("300.00"))
    pid = save_pending(conn, e1, source_filename="1.pdf")
    aid = save_pending(conn, e2, source_filename="2.pdf")
    rid = save_pending(conn, e3, source_filename="3.pdf")
    approve_invoice(conn, aid, reviewer="Bob")
    reject_invoice(conn, rid, reviewer="Bob", review_note="x")
    return pid, aid, rid


def test_tracker_returns_all(temp_conn):
    _seed_three(temp_conn)
    rows = list_payment_tracker(temp_conn)
    assert len(rows) == 3
    expected_keys = {
        "id", "source_filename", "vendor_name", "invoice_number",
        "due_date", "total_amount", "currency", "expense_category",
        "status", "payment_state", "paid_at",
    }
    for r in rows:
        assert expected_keys <= set(r.keys())


def test_tracker_filter_pending(temp_conn):
    _seed_three(temp_conn)
    rows = list_payment_tracker(temp_conn,
                                filter_value=TRACKER_FILTER_PENDING)
    assert len(rows) == 1
    assert rows[0]["status"] == STATUS_PENDING


def test_tracker_filter_approved(temp_conn):
    _seed_three(temp_conn)
    rows = list_payment_tracker(temp_conn,
                                filter_value=TRACKER_FILTER_APPROVED)
    assert len(rows) == 1
    assert rows[0]["status"] == STATUS_APPROVED


def test_tracker_filter_rejected(temp_conn):
    _seed_three(temp_conn)
    rows = list_payment_tracker(temp_conn,
                                filter_value=TRACKER_FILTER_REJECTED)
    assert len(rows) == 1
    assert rows[0]["status"] == STATUS_REJECTED


def test_tracker_filter_outstanding_vs_paid(temp_conn):
    _pid, aid, _rid = _seed_three(temp_conn)
    rows = list_payment_tracker(temp_conn,
                                filter_value=TRACKER_FILTER_OUTSTANDING)
    assert [r["id"] for r in rows] == [aid]
    mark_paid(temp_conn, aid, paid_at="2026-09-22")
    rows = list_payment_tracker(temp_conn,
                                filter_value=TRACKER_FILTER_PAID)
    assert [r["id"] for r in rows] == [aid]


def test_tracker_filter_all_returns_everything(temp_conn):
    _seed_three(temp_conn)
    rows = list_payment_tracker(temp_conn, filter_value=TRACKER_FILTER_ALL)
    assert len(rows) == 3


def test_tracker_filter_unknown_falls_back_to_all(temp_conn):
    _seed_three(temp_conn)
    rows = list_payment_tracker(temp_conn, filter_value="NotARealFilter")
    assert len(rows) == 3


def test_all_tracker_filters_constant_is_complete():
    expected = {
        "All", "Pending", "Approved", "Rejected", "Outstanding", "Paid",
    }
    assert set(ALL_TRACKER_FILTERS) == expected


def test_all_statuses_constant_is_complete():
    assert set(ALL_STATUSES) == {
        STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED,
    }


# duplicates
def test_duplicate_records_are_allowed(temp_conn):
    e1 = _make_extraction(vendor_name="Prairie",
                          invoice_number="PPR-2026-100",
                          total_amount=Decimal("5250.00"))
    e2 = _make_extraction(vendor_name="prairie",
                          invoice_number="PPR-2026-100",
                          total_amount=Decimal("5250.00"))
    id1 = save_pending(temp_conn, e1, source_filename="a.pdf")
    id2 = save_pending(temp_conn, e2, source_filename="b.pdf")
    assert id1 != id2
    rows = list_payment_tracker(temp_conn, filter_value=TRACKER_FILTER_ALL)
    assert len(rows) == 2


def test_duplicate_warning_does_not_auto_reject(temp_conn):
    e1 = _make_extraction(vendor_name="Prairie",
                          invoice_number="PPR-2026-100",
                          total_amount=Decimal("5250.00"))
    save_pending(temp_conn, e1, source_filename="a.pdf")
    e2 = _make_extraction(vendor_name="Prairie",
                          invoice_number="PPR-2026-100",
                          total_amount=Decimal("5250.00"))
    new_id = save_pending(temp_conn, e2, source_filename="b.pdf")
    saved = get_invoice(temp_conn, new_id)
    assert saved.status == STATUS_PENDING


# synthetic E2E
def test_synthetic_step7_e2e_prairie_pump(temp_conn):
    ext = _make_extraction(
        vendor_name="Prairie Pump Rentals",
        invoice_number="PPR-2026-100",
        due_date="2026-09-30",
        total_amount=Decimal("5250.00"),
        currency="CAD",
        description="Equipment rental for wellsite pump.",
    )
    classification = _make_classification("Equipment")
    pid = save_pending(temp_conn, ext, source_filename="ppr.pdf",
                       expense_category=classification.category,
                       classification_source=classification.source,
                       classification=classification,
                       validation=_make_validation(),
                       reviewer="Alice")
    saved = get_invoice(temp_conn, pid)
    assert saved.status == STATUS_PENDING
    assert saved.payment_state == "Not ready"
    saved = approve_invoice(temp_conn, pid, reviewer="Bob")
    assert saved.status == STATUS_APPROVED
    assert saved.payment_state == "Outstanding"
    saved = mark_paid(temp_conn, pid,
                      paid_at="2026-09-22",
                      payment_note="Wire ref WIRE-2026-0922-001")
    assert saved.status == STATUS_APPROVED
    assert saved.paid_at == "2026-09-22"
    assert saved.payment_note == "Wire ref WIRE-2026-0922-001"
    assert saved.payment_state == "Paid"
    rows = list_payment_tracker(temp_conn,
                                filter_value=TRACKER_FILTER_PAID)
    assert len(rows) == 1
    assert rows[0]["id"] == pid


def test_duplicate_e2e_is_allowed_and_reviewable(temp_conn):
    common = dict(vendor_name="Prairie Pump Rentals",
                  invoice_number="PPR-2026-100",
                  total_amount=Decimal("5250.00"))
    e1 = _make_extraction(**common)
    e2 = _make_extraction(**common)
    id1 = save_pending(temp_conn, e1, source_filename="a.pdf")
    id2 = save_pending(temp_conn, e2, source_filename="b.pdf")
    s1 = get_invoice(temp_conn, id1)
    s2 = get_invoice(temp_conn, id2)
    assert s1.status == STATUS_PENDING
    assert s2.status == STATUS_PENDING
    approve_invoice(temp_conn, id1, reviewer="Alice")
    s1 = get_invoice(temp_conn, id1)
    s2 = get_invoice(temp_conn, id2)
    assert s1.status == STATUS_APPROVED
    assert s2.status == STATUS_PENDING


# ---------------------------------------------------------------------------
# STEP 9 - Document traceability tests
# ---------------------------------------------------------------------------

def test_save_pending_accepts_document_ref_fields(temp_conn):
    """save_pending accepts and persists the 3 new document_* fields."""
    ext = _make_extraction()
    new_id = save_pending(
        temp_conn, ext, source_filename="doc.pdf",
        expense_category="Equipment", classification_source="mock",
        document_path="invoice_x__abc__doc.pdf",
        document_sha256="a" * 64,
        document_size_bytes=12345,
    )
    saved = get_invoice(temp_conn, new_id)
    assert saved.document_path == "invoice_x__abc__doc.pdf"
    assert saved.document_sha256 == "a" * 64
    assert saved.document_size_bytes == 12345


def test_save_pending_legacy_callers_default_to_null_doc(temp_conn):
    """When a legacy caller passes nothing for document_*, the columns are NULL."""
    ext = _make_extraction()
    new_id = save_pending(temp_conn, ext, source_filename="legacy.pdf")
    saved = get_invoice(temp_conn, new_id)
    assert saved.document_path is None
    assert saved.document_sha256 is None
    assert saved.document_size_bytes is None


def test_legacy_rows_with_null_document_do_not_break_get_invoice(temp_db_path):
    """A pre-STEP 9 invoice row (NULL document_path) must be readable
    without raising; get_invoice returns None for the new fields."""
    conn = get_connection(temp_db_path)
    try:
        # Insert a row the way pre-STEP 9 code would have.
        conn.execute(
            "INSERT INTO invoices (vendor_name, total_amount, status) "
            "VALUES (?, ?, ?)",
            ("Legacy Vendor", 100.0, STATUS_APPROVED),
        )
        conn.commit()
        row_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        # Reading must not raise.
        saved = get_invoice(conn, row_id)
        assert saved.vendor_name == "Legacy Vendor"
        assert saved.document_path is None
        assert saved.document_sha256 is None
        assert saved.document_size_bytes is None
    finally:
        conn.close()


def test_update_document_ref_attaches_metadata(temp_conn):
    ext = _make_extraction()
    new_id = save_pending(temp_conn, ext, source_filename="orig.pdf")
    # Initially nothing.
    saved = get_invoice(temp_conn, new_id)
    assert saved.document_path is None
    # Now attach via the update helper.
    updated = update_document_ref(
        temp_conn, new_id,
        document_path="invoice_{:d}__deadbeef__orig.pdf".format(new_id),
        document_sha256="f" * 64,
        document_size_bytes=9876,
    )
    assert updated.document_path == "invoice_{:d}__deadbeef__orig.pdf".format(new_id)
    assert updated.document_sha256 == "f" * 64
    assert updated.document_size_bytes == 9876


def test_update_document_ref_noop_when_all_none(temp_conn):
    ext = _make_extraction()
    new_id = save_pending(temp_conn, ext, source_filename="noop.pdf")
    # Should not raise and should leave the row untouched.
    saved = update_document_ref(temp_conn, new_id,
                                document_path=None,
                                document_sha256=None,
                                document_size_bytes=None)
    assert saved.id == new_id
    assert saved.document_path is None
    assert saved.document_sha256 is None
    assert saved.document_size_bytes is None


def test_update_document_ref_then_list_payment_tracker(temp_conn):
    """Tracker view must surface the document reference (or its absence)."""
    from services.invoice_repository import list_payment_tracker
    ext = _make_extraction()
    new_id = save_pending(temp_conn, ext, source_filename="t.pdf")
    update_document_ref(
        temp_conn, new_id,
        document_path="invoice_{:d}__ffff__t.pdf".format(new_id),
        document_sha256="0" * 64,
        document_size_bytes=42,
    )
    rows = list_payment_tracker(temp_conn, filter_value=TRACKER_FILTER_ALL)
    assert len(rows) == 1
    assert rows[0]["id"] == new_id
    # Tracker row exposes source_filename; the document_path lives on the
    # full SavedInvoice object (get_invoice), not on the tracker dict.
    saved = get_invoice(temp_conn, new_id)
    assert saved.document_path == "invoice_{:d}__ffff__t.pdf".format(new_id)
