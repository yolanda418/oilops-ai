"""STEP 10 - Full End-to-End Integration Tests.

Walks synthetic invoices through the COMPLETE STEP 1 - STEP 9 product
pipeline and asserts every privacy / human-control / multi-currency /
payment-safety invariant holds across the COMBINED call chain.

Every fixture here is COMPLETELY FICTIONAL. Tests use a fresh
tmp_path SQLite and never touch database/oilops.db.
"""

from __future__ import annotations

import os
import tempfile
from datetime import date, datetime
from decimal import Decimal

import pytest

from database.db import get_connection, init_db
from services.ai_classifier import (
    MockProvider, build_classifier_payload, classify_expense,
)
from services.dashboard_service import compute_dashboard
from services.invoice_checker import (
    CODE_POSSIBLE_DUPLICATE, check_invoice,
)
from services.invoice_extractor import extract_invoice
from services.invoice_parser import parse_pdf
from services.invoice_repository import (
    InvalidPaymentState, MissingReviewerError, MissingReviewNoteError,
    STATUS_APPROVED, STATUS_PENDING, STATUS_REJECTED,
    approve_invoice, get_invoice, list_payment_tracker,
    mark_paid, reject_invoice, save_pending,
)
from services.privacy_filter import (
    build_ai_safe_payload, is_ai_safe_payload, redact_text,
)
from services.weekly_summary import (
    build_deterministic_weekly_summary, build_weekly_summary_payload,
    compute_weekly_facts,
)

from tests.fixtures.make_e2e_invoices import render_all_pdfs


REFERENCE_DATE = date(2026, 9, 22)


def _init_tmp_db():
    tmpdir = tempfile.mkdtemp(prefix="oilops_e2e_")
    db_path = os.path.join(tmpdir, "e2e.db")
    init_db(db_path)
    return get_connection(db_path), tmpdir


def _run_pipeline_through_save(conn, pdf_path):
    parsed = parse_pdf(pdf_path.read_bytes(), filename=pdf_path.name)
    assert parsed.parsing_status == "ok"
    extraction = extract_invoice(parsed.raw_text)
    payload = build_ai_safe_payload(extraction)
    assert is_ai_safe_payload(payload)
    classification = classify_expense(extraction, provider=MockProvider())
    validation = check_invoice(
        extraction, db_conn=conn, reference_date=REFERENCE_DATE
    )
    invoice_id = save_pending(
        conn, extraction,
        source_filename=pdf_path.name,
        expense_category=classification.category,
        classification_source=classification.source,
        classification=classification,
        validation=validation,
    )
    return {
        "parsed": parsed, "extraction": extraction, "payload": payload,
        "classification": classification, "validation": validation,
        "invoice_id": invoice_id,
    }


def _pin_created_at(conn, invoice_id, ts="2026-09-22 10:00:00"):
    conn.execute(
        "UPDATE invoices SET created_at = ? WHERE id = ?",
        (ts, invoice_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# PHASE 4 - Full happy-path lifecycle E2E (CASE 1).
# ---------------------------------------------------------------------------


def test_full_invoice_lifecycle_e2e(tmp_path):
    conn, _tmpdir = _init_tmp_db()
    try:
        pdfs = render_all_pdfs(tmp_path)
        target = next(p for p in pdfs if "case1" in p.name)

        parsed = parse_pdf(target.read_bytes(), filename=target.name)
        assert parsed.parsing_status == "ok"
        assert parsed.page_count >= 1

        extraction = extract_invoice(parsed.raw_text)
        assert extraction.vendor_name == "Prairie Pump Rentals"
        assert extraction.invoice_number == "PPR-1001"
        assert extraction.total_amount == Decimal("5250.00")
        assert extraction.currency == "CAD"

        payload = build_ai_safe_payload(extraction)
        assert is_ai_safe_payload(payload)
        assert payload["vendor_name"] == "Prairie Pump Rentals"
        assert "bank_account" not in payload
        assert "raw_text" not in payload

        validation = check_invoice(
            extraction, db_conn=conn, reference_date=REFERENCE_DATE
        )
        assert any(i.code == "OVERDUE" for i in validation.issues)
        assert not any(i.code == "MISSING_PO" for i in validation.issues)

        classification = classify_expense(
            extraction, provider=MockProvider()
        )
        assert classification.source == "mock"
        assert classification.category in (
            "Equipment", "Field Services", "Transportation",
            "Office", "Professional Services", "Travel", "Other",
        )

        invoice_id = save_pending(
            conn, extraction,
            source_filename=target.name,
            expense_category=classification.category,
            classification_source=classification.source,
            classification=classification,
            validation=validation,
        )
        assert get_invoice(conn, invoice_id).status == STATUS_PENDING

        approved = approve_invoice(
            conn, invoice_id,
            reviewer="TestApprover",
            review_note="Looks correct.",
        )
        assert approved.status == STATUS_APPROVED
        assert approved.reviewer == "TestApprover"

        tracker = list_payment_tracker(conn)
        matched = [r for r in tracker if r["id"] == invoice_id]
        assert matched and matched[0]["payment_state"] == "Outstanding"

        paid = mark_paid(
            conn, invoice_id,
            paid_at=datetime(2026, 9, 22, 14, 30, 0),
            payment_note="Paid by operator (TEST DATA)",
        )
        assert paid.status == STATUS_APPROVED
        assert paid.paid_at is not None
        assert paid.paid_at.startswith("2026-09-22")

        _pin_created_at(conn, invoice_id)

        snap = compute_dashboard(conn, REFERENCE_DATE, currency="CAD")
        assert snap.kpis.total_invoices == 1
        assert snap.kpis.paid_amount.for_currency("CAD") == Decimal("5250.00")
        assert snap.kpis.outstanding_amount.for_currency("CAD") == Decimal("0")
        assert snap.kpis.approved == 1
        assert snap.kpis.pending_review == 0
        assert snap.kpis.rejected == 0

        facts = compute_weekly_facts(conn, REFERENCE_DATE)
        assert facts.recorded_amounts.get("CAD") == Decimal("5250.00")
        assert facts.paid_this_week_amounts.get("CAD") == Decimal("5250.00")

        summary_text = build_deterministic_weekly_summary(facts)
        assert "Weekly Office Operations Summary" in summary_text
        assert "CAD" in summary_text
        assert "no external payment was executed" in summary_text.lower()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# PHASE 5 - Sensitive invoice privacy E2E (CASE 7).
# ---------------------------------------------------------------------------


def test_sensitive_invoice_privacy_e2e(tmp_path):
    conn, _tmpdir = _init_tmp_db()
    try:
        pdfs = render_all_pdfs(tmp_path)
        sensitive_pdf = next(p for p in pdfs if "case7_sensitive" in p.name)

        parsed = parse_pdf(
            sensitive_pdf.read_bytes(), filename=sensitive_pdf.name
        )
        assert parsed.parsing_status == "ok"
        assert "Bank Account" in parsed.raw_text
        assert "billing@example.com" in parsed.raw_text
        assert "GB82WEST12345698765432" in parsed.raw_text
        assert "(403) 555-0123" in parsed.raw_text

        redacted = redact_text(parsed.raw_text)
        cats = redacted.categories
        assert "BANK_ACCOUNT" in cats
        assert "ROUTING_NUMBER" in cats
        assert "SWIFT" in cats
        assert "IBAN" in cats
        assert "EMAIL" in cats
        assert "PHONE" in cats
        assert "123456789012" not in redacted.redacted_text
        assert "021000021" not in redacted.redacted_text
        assert "BOFMCAM2" not in redacted.redacted_text
        assert "GB82WEST12345698765432" not in redacted.redacted_text
        assert "billing@example.com" not in redacted.redacted_text
        assert "(403) 555-0123" not in redacted.redacted_text

        extraction = extract_invoice(parsed.raw_text)
        payload = build_ai_safe_payload(extraction)
        assert is_ai_safe_payload(payload)
        for forbidden in (
            "bank_account", "routing_number", "swift", "iban",
            "email", "phone", "raw_text",
        ):
            assert forbidden not in payload

        clf_payload = build_classifier_payload(extraction)
        for forbidden in (
            "bank_account", "routing_number", "swift", "iban",
            "email", "phone", "raw_text", "invoice_number", "po_number",
        ):
            assert forbidden not in clf_payload

        validation = check_invoice(
            extraction, db_conn=conn, reference_date=REFERENCE_DATE
        )
        classification = classify_expense(
            extraction, provider=MockProvider()
        )
        invoice_id = save_pending(
            conn, extraction,
            source_filename=sensitive_pdf.name,
            expense_category=classification.category,
            classification_source=classification.source,
            classification=classification,
            validation=validation,
        )
        approve_invoice(
            conn, invoice_id,
            reviewer="TestApprover",
            review_note="Privacy E2E test (SYNTHETIC).",
        )
        mark_paid(
            conn, invoice_id,
            paid_at=datetime(2026, 9, 22, 12, 0, 0),
            payment_note="Synthetic only",
        )
        # Commit so any subsequent inspection observes the persisted row.
        conn.commit()

        # Inspect via the SAME connection - safer for SQLite isolation.
        row = conn.execute(
            "SELECT raw_text, extraction_json, validation_flags "
            "FROM invoices WHERE id = ?",
            (invoice_id,),
        ).fetchone()
        assert row is not None
        assert row["raw_text"] is None
        for forbidden in (
            "123456789012", "021000021", "BOFMCAM2",
            "GB82WEST12345698765432", "billing@example.com",
            "(403) 555-0123",
        ):
            blob = (row["extraction_json"] or "") + (
                row["validation_flags"] or ""
            )
            assert forbidden not in blob

        facts = compute_weekly_facts(conn, REFERENCE_DATE)
        ws_payload = build_weekly_summary_payload(facts)
        for forbidden in (
            "vendor_name", "invoice_number", "po_number",
            "bank_account", "routing_number", "swift", "iban",
            "email", "phone", "raw_text", "reviewer", "review_note",
            "payment_note",
        ):
            assert forbidden not in ws_payload

        narrative = build_deterministic_weekly_summary(facts)
        for forbidden in (
            "123456789012", "021000021", "BOFMCAM2",
            "GB82WEST12345698765432", "billing@example.com",
            "(403) 555-0123",
            "Prairie Field Equipment",
            "PO-8821", "PFE-2026-009",
        ):
            assert forbidden not in narrative
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# PHASE 6 - Duplicate pair E2E (CASES 4 + 5).
# ---------------------------------------------------------------------------


def test_duplicate_pair_e2e(tmp_path):
    conn, _tmpdir = _init_tmp_db()
    try:
        pdfs = render_all_pdfs(tmp_path)
        run4 = _run_pipeline_through_save(
            conn, next(p for p in pdfs if "case4_duplicate_a" in p.name),
        )
        run5 = _run_pipeline_through_save(
            conn, next(p for p in pdfs if "case5_duplicate_b" in p.name),
        )
        assert run4["invoice_id"] != run5["invoice_id"]
        assert run4["extraction"].invoice_number == "DUP-001"
        assert run5["extraction"].invoice_number == "DUP-001"
        assert run4["extraction"].total_amount == Decimal("1500.00")
        assert run5["extraction"].total_amount == Decimal("1500.00")

        cur = conn.execute(
            "SELECT COUNT(*) FROM invoices WHERE invoice_number = ?",
            ("DUP-001",),
        )
        assert cur.fetchone()[0] == 2, (
            "DB must allow duplicate rows for human review."
        )

        codes_5 = {i.code for i in run5["validation"].issues}
        assert CODE_POSSIBLE_DUPLICATE in codes_5

        snap = compute_dashboard(conn, REFERENCE_DATE, currency="CAD")
        assert snap.kpis.possible_duplicate_groups == 1
        assert len(snap.duplicate_groups) == 1
        assert snap.duplicate_groups[0].invoice_count == 2

        for inv_id in (run4["invoice_id"], run5["invoice_id"]):
            approve_invoice(
                conn, inv_id,
                reviewer="TestApprover",
                review_note="Duplicate acknowledged by human.",
            )
            mark_paid(
                conn, inv_id,
                paid_at=datetime(2026, 9, 22, 13, 0, 0),
                payment_note="Duplicate pair - human handled.",
            )
        conn.commit()
        cur = conn.execute("SELECT COUNT(*) FROM invoices")
        assert cur.fetchone()[0] == 2
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# PHASE 10 - Multi-currency E2E (CAD + USD, never summed together).
# ---------------------------------------------------------------------------


def test_multi_currency_e2e(tmp_path):
    conn, _tmpdir = _init_tmp_db()
    try:
        pdfs = render_all_pdfs(tmp_path)
        cad_run = _run_pipeline_through_save(
            conn, next(p for p in pdfs if "case1_clean" in p.name),
        )
        usd_run = _run_pipeline_through_save(
            conn, next(p for p in pdfs if "case6_usd" in p.name),
        )
        paid_run = _run_pipeline_through_save(
            conn, next(p for p in pdfs if "case2_paid" in p.name),
        )

        assert cad_run["extraction"].currency == "CAD"
        assert usd_run["extraction"].currency == "USD"
        assert paid_run["extraction"].currency == "CAD"

        for inv_id in (
            cad_run["invoice_id"], usd_run["invoice_id"],
            paid_run["invoice_id"],
        ):
            approve_invoice(
                conn, inv_id,
                reviewer="TestApprover",
                review_note="Multi-currency approve.",
            )

        mark_paid(
            conn, paid_run["invoice_id"],
            paid_at=datetime(2026, 9, 22, 11, 0, 0),
            payment_note="Multi-currency test - paid (SYNTHETIC).",
        )
        _pin_created_at(conn, paid_run["invoice_id"])
        _pin_created_at(conn, cad_run["invoice_id"])
        _pin_created_at(conn, usd_run["invoice_id"])

        # CAD view: case1 outstanding CAD 5250 + case2 paid CAD 10500.
        # total_invoices is the global row count; the per-currency amounts
        # are what the currency filter actually narrows.
        snap_cad = compute_dashboard(conn, REFERENCE_DATE, currency="CAD")
        assert snap_cad.kpis.total_invoices == 3
        assert snap_cad.kpis.outstanding_amount.for_currency("CAD") == Decimal(
            "5250.00"
        )
        assert snap_cad.kpis.paid_amount.for_currency("CAD") == Decimal(
            "10500.00"
        )
        # USD NEVER present in CAD-only amount views.
        assert snap_cad.kpis.outstanding_amount.for_currency("USD") == Decimal(
            "0"
        )
        assert snap_cad.kpis.paid_amount.for_currency("USD") == Decimal("0")
        assert "USD" not in snap_cad.kpis.outstanding_amount.amounts
        assert "USD" not in snap_cad.kpis.paid_amount.amounts

        # USD view: only the one USD outstanding invoice contributes to
        # the per-currency amounts. The CAD paid invoice is excluded from
        # the paid_amount dict.
        snap_usd = compute_dashboard(conn, REFERENCE_DATE, currency="USD")
        assert snap_usd.kpis.total_invoices == 3
        assert snap_usd.kpis.outstanding_amount.for_currency("USD") == Decimal(
            "3000.00"
        )
        assert snap_usd.kpis.paid_amount.for_currency("USD") == Decimal("0")
        assert "CAD" not in snap_usd.kpis.outstanding_amount.amounts
        assert "CAD" not in snap_usd.kpis.paid_amount.amounts

        # Weekly facts: CAD and USD stored as separate keys.
        facts = compute_weekly_facts(conn, REFERENCE_DATE)
        assert facts.recorded_amounts.get("CAD") == Decimal("15750.00")
        assert facts.recorded_amounts.get("USD") == Decimal("3000.00")
        assert facts.paid_this_week_amounts.get("CAD") == Decimal("10500.00")
        # No USD was paid this week.
        assert facts.paid_this_week_amounts.get("USD", Decimal("0")) == Decimal(
            "0"
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# PHASE 11 - Human control E2E (no auto-approve, reviewer required).
# ---------------------------------------------------------------------------


def test_human_control_e2e(tmp_path):
    conn, _tmpdir = _init_tmp_db()
    try:
        pdfs = render_all_pdfs(tmp_path)
        target = next(p for p in pdfs if "case1_clean" in p.name)
        run = _run_pipeline_through_save(conn, target)
        invoice_id = run["invoice_id"]

        # 1. After save_pending, status is PENDING and nothing auto-flipped.
        pending_row = get_invoice(conn, invoice_id)
        assert pending_row.status == STATUS_PENDING
        assert pending_row.reviewer is None
        assert pending_row.review_note is None

        # 2. AI classification must NOT have set status to APPROVED.
        assert run["classification"].source == "mock"
        assert run["classification"].category is not None
        assert pending_row.status == STATUS_PENDING, (
            "AI classification must never auto-approve."
        )

        # 3. Validation issues must NOT have auto-rejected the invoice.
        #    Only a row with REVIEWER + NOTE can transition to rejected.
        assert pending_row.status != STATUS_REJECTED
        with pytest.raises(MissingReviewerError):
            approve_invoice(conn, invoice_id, reviewer="")
        with pytest.raises(MissingReviewerError):
            reject_invoice(conn, invoice_id, reviewer="", review_note="x")
        with pytest.raises(MissingReviewNoteError):
            reject_invoice(
                conn, invoice_id, reviewer="TestApprover", review_note=""
            )

        # 4. Approve requires a reviewer.
        with pytest.raises(MissingReviewerError):
            approve_invoice(conn, invoice_id, reviewer="   ")

        # 5. Reject requires BOTH reviewer AND note.
        with pytest.raises(MissingReviewNoteError):
            reject_invoice(
                conn, invoice_id,
                reviewer="TestApprover", review_note="   ",
            )

        # 6. The happy path: explicit human approve.
        approved = approve_invoice(
            conn, invoice_id,
            reviewer="TestApprover",
            review_note="Human-approved for STEP 10.",
        )
        assert approved.status == STATUS_APPROVED
        assert approved.reviewer == "TestApprover"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# PHASE 12 - Payment safety E2E (mark paid is paid_at tracking only,
#             and only approved invoices can be paid).
# ---------------------------------------------------------------------------


def test_payment_safety_e2e(tmp_path):
    conn, _tmpdir = _init_tmp_db()
    try:
        pdfs = render_all_pdfs(tmp_path)
        case1 = next(p for p in pdfs if "case1_clean" in p.name)
        case3 = next(p for p in pdfs if "case3_missing_po" in p.name)
        run_ok = _run_pipeline_through_save(conn, case1)
        run_pending = _run_pipeline_through_save(conn, case3)

        # 1. Pending invoice CANNOT be marked paid.
        with pytest.raises(InvalidPaymentState):
            mark_paid(
                conn, run_pending["invoice_id"],
                paid_at=datetime(2026, 9, 22, 12, 0, 0),
                payment_note="Should fail - pending.",
            )
        still_pending = get_invoice(conn, run_pending["invoice_id"])
        assert still_pending.status == STATUS_PENDING
        assert still_pending.paid_at is None

        # 2. Rejected invoice CANNOT be marked paid.
        reject_invoice(
            conn, run_pending["invoice_id"],
            reviewer="TestApprover",
            review_note="Rejected for safety test.",
        )
        with pytest.raises(InvalidPaymentState):
            mark_paid(
                conn, run_pending["invoice_id"],
                paid_at=datetime(2026, 9, 22, 12, 0, 0),
                payment_note="Should fail - rejected.",
            )
        assert get_invoice(conn, run_pending["invoice_id"]).status == (
            STATUS_REJECTED
        )

        # 3. Approved invoice CAN be marked paid (only paid_at tracking,
        #    NO payment execution).
        approve_invoice(
            conn, run_ok["invoice_id"],
            reviewer="TestApprover",
            review_note="Approve for safety test.",
        )
        paid = mark_paid(
            conn, run_ok["invoice_id"],
            paid_at=datetime(2026, 9, 22, 13, 0, 0),
            payment_note="Synthetic only.",
        )
        assert paid.paid_at is not None
        assert paid.paid_at.startswith("2026-09-22")
        # No bank / wire / ACH / stripe / payment API terms persisted.
        forbidden_in_db = conn.execute(
            "SELECT review_note, payment_note FROM invoices WHERE id = ?",
            (run_ok["invoice_id"],),
        ).fetchone()
        joined = (forbidden_in_db["review_note"] or "") + (
            forbidden_in_db["payment_note"] or ""
        )
        for forbidden in (
            "ach", "wire", "swift_transfer", "stripe",
            "bank_api", "eft", "execute_payment",
        ):
            assert forbidden not in joined.lower(), (
                "No payment execution should be persisted."
            )

        # 4. Marking paid twice on the same invoice raises.
        with pytest.raises(InvalidPaymentState):
            mark_paid(
                conn, run_ok["invoice_id"],
                paid_at=datetime(2026, 9, 22, 14, 0, 0),
                payment_note="Double pay.",
            )
    finally:
        conn.close()
