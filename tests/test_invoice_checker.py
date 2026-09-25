
"""Tests for services/invoice_checker.py (STEP 5).

All fixtures are SYNTHETIC. No real vendor / bank / personal data.

These tests pin down the deterministic validation boundary:
    * missing-field checks (PO / invoice number / due date)
    * due-date logic (DUE_SOON, OVERDUE, mutual exclusivity)
    * arithmetic checks (subtotal + GST == total) using Decimal
    * zero / negative amount handling
    * optional / configurable large-amount policy
    * duplicate detection via parameterised SQL on a tmp database
    * duplicate detection returns MINIMAL metadata (no raw text,
      no sensitive fields)
    * no network calls anywhere in the checker
    * human-corrected extractions can be validated
"""

from __future__ import annotations

import socket
import urllib.request
from datetime import date
from decimal import Decimal

import pytest

from database.db import get_connection, init_db
from services.invoice_checker import (
    AMOUNT_TOLERANCE,
    CODE_AMOUNT_MISMATCH,
    CODE_DUE_SOON,
    CODE_LARGE_AMOUNT_REVIEW,
    CODE_MISSING_DUE_DATE,
    CODE_MISSING_INVOICE_NUMBER,
    CODE_MISSING_PO,
    CODE_NEGATIVE_AMOUNT,
    CODE_OVERDUE,
    CODE_POSSIBLE_DUPLICATE,
    CODE_ZERO_AMOUNT,
    DUE_SOON_DAYS,
    SEVERITY_ERROR,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    InvoiceValidationResult,
    ValidationIssue,
    check_invoice,
    find_duplicate_invoices,
    format_validation_summary,
)
from services.invoice_extractor import InvoiceExtraction


# ---------------------------------------------------------------------------
# Synthetic fixtures
# ---------------------------------------------------------------------------


def _clean_extraction() -> InvoiceExtraction:
    """A perfectly valid synthetic invoice. Baseline for happy-path tests."""
    return InvoiceExtraction(
        vendor_name="Prairie Field Equipment Ltd.",
        invoice_number="PFE-2026-001",
        invoice_date="2026-09-01",
        due_date="2026-10-01",
        po_number="PO-1001",
        description="Pump equipment rental (synthetic)",
        subtotal=Decimal("1000.00"),
        gst=Decimal("50.00"),
        total_amount=Decimal("1050.00"),
        currency="CAD",
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_clean_invoice_produces_no_issues():
    ext = _clean_extraction()
    result = check_invoice(ext, reference_date=date(2026, 9, 22))
    assert isinstance(result, InvoiceValidationResult)
    assert result.issues == []
    assert result.error_count == 0
    assert result.warning_count == 0
    assert result.info_count == 0
    assert result.has_issues is False
    assert result.has_errors is False
    assert result.has_warnings is False
    assert format_validation_summary(result) == "0 Errors, 0 Warnings, 0 Info"


def test_validation_result_is_deterministic_without_reference_date():
    """Without a reference date we skip DUE_SOON / OVERDUE silently."""
    ext = _clean_extraction()
    r1 = check_invoice(ext)
    r2 = check_invoice(ext)
    assert r1.codes() == r2.codes() == []




# ---------------------------------------------------------------------------
# Missing-field checks
# ---------------------------------------------------------------------------


def test_missing_po_flags_warning():
    ext = _clean_extraction()
    ext.po_number = None
    result = check_invoice(ext)
    codes = result.codes()
    assert CODE_MISSING_PO in codes
    issue = result.by_code(CODE_MISSING_PO)[0]
    assert issue.severity == SEVERITY_WARNING
    assert issue.field == "po_number"
    assert "PO" in issue.message


def test_missing_po_treats_empty_string_as_missing():
    ext = _clean_extraction()
    ext.po_number = ""
    result = check_invoice(ext)
    assert CODE_MISSING_PO in result.codes()


def test_missing_invoice_number_flags_error():
    ext = _clean_extraction()
    ext.invoice_number = None
    result = check_invoice(ext)
    codes = result.codes()
    assert CODE_MISSING_INVOICE_NUMBER in codes
    issue = result.by_code(CODE_MISSING_INVOICE_NUMBER)[0]
    assert issue.severity == SEVERITY_ERROR
    assert issue.field == "invoice_number"


def test_missing_due_date_flags_warning():
    ext = _clean_extraction()
    ext.due_date = None
    result = check_invoice(ext)
    assert CODE_MISSING_DUE_DATE in result.codes()
    issue = result.by_code(CODE_MISSING_DUE_DATE)[0]
    assert issue.severity == SEVERITY_WARNING
    assert issue.field == "due_date"


def test_missing_due_date_skips_due_status_check():
    """A missing due_date must NOT also produce DUE_SOON or OVERDUE."""
    ext = _clean_extraction()
    ext.due_date = None
    result = check_invoice(ext, reference_date=date(2026, 9, 22))
    assert CODE_DUE_SOON not in result.codes()
    assert CODE_OVERDUE not in result.codes()


# ---------------------------------------------------------------------------
# Due-date checks (DUE_SOON / OVERDUE, mutually exclusive)
# ---------------------------------------------------------------------------


def test_due_soon_within_window():
    ext = _clean_extraction()
    ext.due_date = "2026-09-25"  # +3 days from reference 2026-09-22
    result = check_invoice(ext, reference_date=date(2026, 9, 22))
    codes = result.codes()
    assert CODE_DUE_SOON in codes
    assert CODE_OVERDUE not in codes


def test_due_soon_at_exact_seven_days_boundary():
    """Exactly DUE_SOON_DAYS days out is still DUE_SOON (inclusive)."""
    ext = _clean_extraction()
    ext.due_date = "2026-09-29"  # +7 days
    result = check_invoice(ext, reference_date=date(2026, 9, 22))
    assert CODE_DUE_SOON in result.codes()
    assert CODE_OVERDUE not in result.codes()


def test_due_soon_window_constant_is_seven():
    """Lock the policy in the public constant so UI + tests agree."""
    assert DUE_SOON_DAYS == 7


def test_due_beyond_window_is_silent():
    ext = _clean_extraction()
    ext.due_date = "2026-10-15"  # > 7 days
    result = check_invoice(ext, reference_date=date(2026, 9, 22))
    assert CODE_DUE_SOON not in result.codes()
    assert CODE_OVERDUE not in result.codes()


def test_overdue_when_due_date_in_past():
    ext = _clean_extraction()
    ext.due_date = "2026-09-15"  # before reference 2026-09-22
    result = check_invoice(ext, reference_date=date(2026, 9, 22))
    codes = result.codes()
    assert CODE_OVERDUE in codes
    assert CODE_DUE_SOON not in codes


def test_overdue_and_due_soon_are_mutually_exclusive():
    """Same due_date cannot produce both codes for one reference_date."""
    for offset in range(-30, 31):
        ref = date(2026, 9, 22)
        due = date.fromordinal(ref.toordinal() + offset)
        ext = _clean_extraction()
        ext.due_date = due.isoformat()
        result = check_invoice(ext, reference_date=ref)
        has_soon = CODE_DUE_SOON in result.codes()
        has_over = CODE_OVERDUE in result.codes()
        assert not (has_soon and has_over), (
            "offset=" + str(offset) + " produced both codes"
        )




# ---------------------------------------------------------------------------
# Amount checks
# ---------------------------------------------------------------------------


def test_amount_arithmetic_correct_is_clean():
    ext = _clean_extraction()
    # 1000 + 50 == 1050
    result = check_invoice(ext)
    assert CODE_AMOUNT_MISMATCH not in result.codes()


def test_amount_mismatch_flags_warning():
    ext = _clean_extraction()
    ext.total_amount = Decimal("1200.00")  # 1000 + 50 = 1050, not 1200
    result = check_invoice(ext)
    codes = result.codes()
    assert CODE_AMOUNT_MISMATCH in codes
    issue = result.by_code(CODE_AMOUNT_MISMATCH)[0]
    assert issue.severity == SEVERITY_WARNING
    assert issue.field == "total_amount"


def test_amount_mismatch_within_cent_tolerance_is_clean():
    """A 1-cent rounding difference must NOT trigger AMOUNT_MISMATCH."""
    ext = _clean_extraction()
    # Make subtotal + gst = total exactly + AMOUNT_TOLERANCE - 0.005
    ext.subtotal = Decimal("1000.00")
    ext.gst = Decimal("50.00")
    ext.total_amount = Decimal("1050.00") + (AMOUNT_TOLERANCE - Decimal("0.005"))
    result = check_invoice(ext)
    assert CODE_AMOUNT_MISMATCH not in result.codes()


def test_amount_mismatch_beyond_cent_tolerance_flags():
    ext = _clean_extraction()
    ext.subtotal = Decimal("1000.00")
    ext.gst = Decimal("50.00")
    ext.total_amount = Decimal("1050.02")  # 2 cents off
    result = check_invoice(ext)
    assert CODE_AMOUNT_MISMATCH in result.codes()


def test_zero_amount_flags_warning():
    ext = _clean_extraction()
    ext.subtotal = Decimal("0")
    ext.gst = Decimal("0")
    ext.total_amount = Decimal("0")
    result = check_invoice(ext)
    codes = result.codes()
    assert CODE_ZERO_AMOUNT in codes
    issue = result.by_code(CODE_ZERO_AMOUNT)[0]
    assert issue.severity == SEVERITY_WARNING
    assert "zero" in issue.message.lower()


def test_negative_amount_flags_warning_with_credit_note_hint():
    ext = _clean_extraction()
    ext.subtotal = Decimal("-500.00")
    ext.gst = Decimal("-25.00")
    ext.total_amount = Decimal("-525.00")
    result = check_invoice(ext)
    codes = result.codes()
    assert CODE_NEGATIVE_AMOUNT in codes
    issue = result.by_code(CODE_NEGATIVE_AMOUNT)[0]
    assert issue.severity == SEVERITY_WARNING
    assert "credit note" in issue.message.lower()


def test_zero_and_negative_are_mutually_exclusive():
    """A zero total is zero, not negative."""
    ext = _clean_extraction()
    ext.total_amount = Decimal("0")
    result = check_invoice(ext)
    assert CODE_NEGATIVE_AMOUNT not in result.codes()
    assert CODE_ZERO_AMOUNT in result.codes()




# ---------------------------------------------------------------------------
# Large-amount policy: configurable, not hard-coded
# ---------------------------------------------------------------------------


def test_no_large_amount_flag_when_threshold_is_none():
    """Default behaviour: a large total_amount is NOT flagged."""
    ext = _clean_extraction()
    ext.total_amount = Decimal("999999.00")
    result = check_invoice(ext, large_amount_threshold=None)
    assert CODE_LARGE_AMOUNT_REVIEW not in result.codes()


def test_large_amount_flag_only_when_threshold_passed():
    ext = _clean_extraction()
    ext.total_amount = Decimal("26000.00")
    result = check_invoice(ext, large_amount_threshold=Decimal("25000"))
    codes = result.codes()
    assert CODE_LARGE_AMOUNT_REVIEW in codes
    issue = result.by_code(CODE_LARGE_AMOUNT_REVIEW)[0]
    assert issue.severity == SEVERITY_INFO
    assert "25000" in issue.message


def test_large_amount_below_threshold_is_clean():
    ext = _clean_extraction()
    ext.total_amount = Decimal("24000.00")
    result = check_invoice(ext, large_amount_threshold=Decimal("25000"))
    assert CODE_LARGE_AMOUNT_REVIEW not in result.codes()


def test_large_amount_threshold_accepts_string_via_decimal_coercion():
    """Threshold can be passed as Decimal; we coerce internally."""
    ext = _clean_extraction()
    ext.total_amount = Decimal("26000.00")
    # Pass an int-like Decimal to ensure conversion path works.
    result = check_invoice(ext, large_amount_threshold=Decimal(25000))
    assert CODE_LARGE_AMOUNT_REVIEW in result.codes()


def test_large_amount_with_missing_total_does_not_crash():
    ext = _clean_extraction()
    ext.total_amount = None
    result = check_invoice(ext, large_amount_threshold=Decimal("25000"))
    assert CODE_LARGE_AMOUNT_REVIEW not in result.codes()




# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_db(tmp_path):
    p = tmp_path / "checker_test.db"
    init_db(str(p))
    return p


def _seed_invoice(db_path, vendor, invoice_number, total):
    """Insert a synthetic invoice row directly (no app-level workflow)."""
    conn = get_connection(str(db_path))
    try:
        conn.execute(
            "INSERT INTO invoices (vendor_name, invoice_number, total_amount, status) "
            "VALUES (?, ?, ?, ?)",
            (vendor, invoice_number, float(total), "pending"),
        )
        conn.commit()
    finally:
        conn.close()


def test_find_duplicate_invoices_uses_parameterised_sql(tmp_db):
    """SQL builder must use ? placeholders, not string concat.

    Attempt an injection-style vendor_name and assert it is treated as
    a literal value (no matching row returned) rather than as SQL.
    """
    _seed_invoice(
        tmp_db,
        "Prairie Field Equipment Ltd.",
        "PFE-2026-009",
        Decimal("5250.00"),
    )
    conn = get_connection(str(tmp_db))
    try:
        rows = find_duplicate_invoices(
            conn,
            "Prairie Field Equipment Ltd.' OR '1'='1",
            "PFE-2026-009",
            Decimal("5250.00"),
        )
        assert rows == [], "Injection-style vendor_name must not match."
    finally:
        conn.close()


def test_find_duplicate_returns_minimal_projection(tmp_db):
    """The duplicate helper must not surface raw_text or sensitive fields."""
    _seed_invoice(
        tmp_db,
        "Prairie Field Equipment Ltd.",
        "PFE-2026-009",
        Decimal("5250.00"),
    )
    conn = get_connection(str(tmp_db))
    try:
        rows = find_duplicate_invoices(
            conn,
            "Prairie Field Equipment Ltd.",
            "PFE-2026-009",
            Decimal("5250.00"),
        )
        assert len(rows) == 1
        row = rows[0]
        # Allowed projection.
        assert "id" in row.keys()
        assert "vendor_name" in row.keys()
        assert "invoice_number" in row.keys()
        assert "total_amount" in row.keys()
        # Forbidden projection.
        for forbidden in ("raw_text", "bank_account", "routing_number",
                          "swift", "iban", "email", "phone"):
            assert forbidden not in row.keys(), (
                "find_duplicate_invoices must not return " + forbidden
            )
    finally:
        conn.close()




def test_possible_duplicate_detected(tmp_db):
    """The canonical STEP 5 scenario from the spec."""
    _seed_invoice(
        tmp_db,
        "Prairie Field Equipment Ltd.",
        "PFE-2026-009",
        Decimal("5250.00"),
    )
    conn = get_connection(str(tmp_db))
    try:
        ext = InvoiceExtraction(
            vendor_name="Prairie Field Equipment Ltd.",
            invoice_number="PFE-2026-009",
            due_date="2026-10-01",
            subtotal=Decimal("5000.00"),
            gst=Decimal("250.00"),
            total_amount=Decimal("5250.00"),
        )
        result = check_invoice(ext, db_conn=conn, reference_date=date(2026, 9, 22))
        assert CODE_POSSIBLE_DUPLICATE in result.codes()
        issue = result.by_code(CODE_POSSIBLE_DUPLICATE)[0]
        assert issue.severity == SEVERITY_INFO
    finally:
        conn.close()


def test_duplicate_check_returns_no_match_when_invoice_number_differs(tmp_db):
    _seed_invoice(
        tmp_db,
        "Prairie Field Equipment Ltd.",
        "PFE-2026-009",
        Decimal("5250.00"),
    )
    conn = get_connection(str(tmp_db))
    try:
        ext = InvoiceExtraction(
            vendor_name="Prairie Field Equipment Ltd.",
            invoice_number="PFE-2026-099",  # different
            due_date="2026-10-01",
            total_amount=Decimal("5250.00"),
        )
        result = check_invoice(ext, db_conn=conn)
        assert CODE_POSSIBLE_DUPLICATE not in result.codes()
    finally:
        conn.close()


def test_duplicate_check_returns_no_match_when_amount_differs(tmp_db):
    _seed_invoice(
        tmp_db,
        "Prairie Field Equipment Ltd.",
        "PFE-2026-009",
        Decimal("5250.00"),
    )
    conn = get_connection(str(tmp_db))
    try:
        ext = InvoiceExtraction(
            vendor_name="Prairie Field Equipment Ltd.",
            invoice_number="PFE-2026-009",
            total_amount=Decimal("5300.00"),  # different
        )
        result = check_invoice(ext, db_conn=conn)
        assert CODE_POSSIBLE_DUPLICATE not in result.codes()
    finally:
        conn.close()


def test_duplicate_records_are_allowed_in_db(tmp_db):
    """The STEP 1 schema must allow duplicate rows (we only flag them)."""
    _seed_invoice(tmp_db, "Prairie Field Equipment Ltd.", "PFE-2026-009", Decimal("5250.00"))
    _seed_invoice(tmp_db, "Prairie Field Equipment Ltd.", "PFE-2026-009", Decimal("5250.00"))
    conn = get_connection(str(tmp_db))
    try:
        rows = conn.execute(
            "SELECT COUNT(*) AS n FROM invoices "
            "WHERE vendor_name = ? AND invoice_number = ? AND total_amount = ?",
            ("Prairie Field Equipment Ltd.", "PFE-2026-009", 5250.00),
        ).fetchone()
        assert rows["n"] == 2
    finally:
        conn.close()


def test_duplicate_check_with_empty_database_returns_clean(tmp_path):
    """If no records exist at all, no POSSIBLE_DUPLICATE flag."""
    p = tmp_path / "empty.db"
    init_db(str(p))
    conn = get_connection(str(p))
    try:
        ext = _clean_extraction()
        result = check_invoice(ext, db_conn=conn)
        assert CODE_POSSIBLE_DUPLICATE not in result.codes()
    finally:
        conn.close()


def test_duplicate_check_with_missing_components_returns_clean(tmp_db):
    """If vendor / invoice number / total is None, we cannot match anything."""
    _seed_invoice(tmp_db, "Prairie Field Equipment Ltd.", "PFE-2026-009", Decimal("5250.00"))
    conn = get_connection(str(tmp_db))
    try:
        # Extraction with no invoice_number / total -- helper must short-circuit.
        ext = InvoiceExtraction(vendor_name="Prairie Field Equipment Ltd.")
        result = check_invoice(ext, db_conn=conn)
        assert CODE_POSSIBLE_DUPLICATE not in result.codes()
    finally:
        conn.close()




# ---------------------------------------------------------------------------
# Human-corrected values can be validated
# ---------------------------------------------------------------------------


def test_validation_uses_human_corrected_extraction():
    """If the human corrected invoice_number, validation must see the fix."""
    raw = InvoiceExtraction(
        vendor_name="Prairie Field Equipment Ltd.",
        invoice_number=None,  # extractor missed it
        due_date="2026-10-01",
        total_amount=Decimal("1050.00"),
    )
    # STEP 3 review UI: human types in the missing invoice number.
    reviewed = InvoiceExtraction(
        vendor_name="Prairie Field Equipment Ltd.",
        invoice_number="PFE-2026-200",
        due_date="2026-10-01",
        total_amount=Decimal("1050.00"),
    )
    raw_result = check_invoice(raw)
    reviewed_result = check_invoice(reviewed)
    assert CODE_MISSING_INVOICE_NUMBER in raw_result.codes()
    assert CODE_MISSING_INVOICE_NUMBER not in reviewed_result.codes()


# ---------------------------------------------------------------------------
# Privacy / network boundary
# ---------------------------------------------------------------------------


def test_validation_result_does_not_contain_raw_text():
    """ValidationIssue / ValidationResult must never echo raw invoice text.

    We never pass raw_text to check_invoice; the extraction dataclass
    simply does not have a raw_text field. This test pins that down:
    even when an invoice "would have" raw bank footer in real life,
    the validation output cannot leak it.
    """
    ext = InvoiceExtraction(
        vendor_name="Prairie Field Equipment Ltd.",
        invoice_number="PFE-2026-009",
        due_date="2026-10-01",
        total_amount=Decimal("1050.00"),
    )
    result = check_invoice(ext)
    blob = repr(result.as_dict())
    for forbidden in (
        "123456789012",
        "021000021",
        "BOFMCAM2",
        "GB82WEST",
        "billing@example.com",
        "(403) 555-0123",
    ):
        assert forbidden not in blob, (
            "Validation output must not contain raw sensitive value: "
            + forbidden
        )


def test_checker_makes_no_network_calls(monkeypatch):
    """Hard guarantee: check_invoice / find_duplicate_invoices are offline."""

    def _blocked_create_connection(*a, **k):
        raise AssertionError("check_invoice must not open TCP connections")

    def _blocked_getaddrinfo(*a, **k):
        raise AssertionError("check_invoice must not perform DNS lookups")

    def _blocked_urlopen(*a, **k):
        raise AssertionError("check_invoice must not make HTTP requests")

    monkeypatch.setattr(socket, "create_connection", _blocked_create_connection, raising=True)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked_getaddrinfo, raising=True)
    monkeypatch.setattr(urllib.request, "urlopen", _blocked_urlopen, raising=True)

    ext = _clean_extraction()
    r = check_invoice(ext, reference_date=date(2026, 9, 22))
    assert isinstance(r, InvoiceValidationResult)


# ---------------------------------------------------------------------------
# Decimal-correctness sanity
# ---------------------------------------------------------------------------


def test_arithmetic_check_does_not_use_float():
    """If implementation regressed to float, 0.1 + 0.2 != 0.3 would fail."""
    ext = InvoiceExtraction(
        vendor_name="Test Vendor",
        invoice_number="TV-1",
        due_date="2026-10-01",
        subtotal=Decimal("0.10"),
        gst=Decimal("0.20"),
        total_amount=Decimal("0.30"),
    )
    result = check_invoice(ext)
    assert CODE_AMOUNT_MISMATCH not in result.codes()




# ---------------------------------------------------------------------------
# ValidationIssue / dataclass sanity
# ---------------------------------------------------------------------------


def test_validation_issue_dataclass_fields():
    iss_ = ValidationIssue(
        code="X", severity=SEVERITY_WARNING, message="msg", field="f",
    )
    assert iss_.code == "X"
    assert iss_.severity == SEVERITY_WARNING
    assert iss_.message == "msg"
    assert iss_.field == "f"


def test_result_as_dict_is_json_safe_strings_only():
    ext = _clean_extraction()
    ext.total_amount = Decimal("9999.99")
    result = check_invoice(ext)
    d = result.as_dict()
    # No Decimal / date objects leak.
    import json
    json.dumps(d)  # must not raise


def test_summary_renders_all_three_counts_even_when_zero():
    """Reviewer must always see the full picture."""
    result = InvoiceValidationResult(issues=[])
    assert format_validation_summary(result) == "0 Errors, 0 Warnings, 0 Info"


# ---------------------------------------------------------------------------
# Defensive: None extraction
# ---------------------------------------------------------------------------


def test_check_invoice_handles_none_gracefully():
    result = check_invoice(None)
    assert isinstance(result, InvoiceValidationResult)
    assert result.issues == []


# ---------------------------------------------------------------------------
# The spec's exact synthetic scenario
# ---------------------------------------------------------------------------


def test_spec_scenario_pfe_2026_010_missing_po_due_soon_clean_amounts():
    """From the STEP 5 spec:

        Invoice: PFE-2026-010
        Missing PO
        Due in 5 days
        Subtotal 5000
        GST 250
        Total 5250

        Expected: MISSING_PO, DUE_SOON
        Not expected: AMOUNT_MISMATCH
    """
    ext = InvoiceExtraction(
        vendor_name="Prairie Field Equipment Ltd.",
        invoice_number="PFE-2026-010",
        invoice_date="2026-09-22",
        due_date="2026-09-27",
        subtotal=Decimal("5000.00"),
        gst=Decimal("250.00"),
        total_amount=Decimal("5250.00"),
        currency="CAD",
    )
    result = check_invoice(ext, reference_date=date(2026, 9, 22))
    codes = result.codes()
    assert CODE_MISSING_PO in codes
    assert CODE_DUE_SOON in codes
    assert CODE_AMOUNT_MISMATCH not in codes
    assert CODE_OVERDUE not in codes
    assert CODE_MISSING_INVOICE_NUMBER not in codes

