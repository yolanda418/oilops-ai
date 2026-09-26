from __future__ import annotations
import streamlit as st
from datetime import date, datetime, datetime

from database.db import (
    init_db,
    get_connection,
    resolve_db_path,
    transaction,
)

init_db()

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from database.models import (
    list_tables,
    list_indexes,
)
from database.migrations import get_applied_version

from services.weekly_summary import (
    compute_weekly_facts,
    build_weekly_summary_payload,
    assert_weekly_summary_payload_safe,
    build_deterministic_weekly_summary,
    generate_weekly_narrative,
    compute_weekly_payload_fingerprint,
    is_narrative_api_key_configured,
)
from services.dashboard_service import (
    compute_dashboard,
    CURRENCY_FILTER_ALL,
    ALLOWED_CURRENCY_FILTERS,
)
from services.invoice_parser import parse_pdf, preview_text
from services.invoice_extractor import extract_invoice, InvoiceExtraction
from services.privacy_filter import (
    redact_text,
    build_ai_safe_payload,
    is_ai_safe_payload,
)
from services.invoice_checker import (
    check_invoice,
    format_validation_summary,
    CODE_POSSIBLE_DUPLICATE,
    CODE_AMOUNT_MISMATCH,
)
from services.ai_classifier import (
    classify_expense,
    format_classification_summary,
    is_api_key_configured,
    CATEGORY_OTHER,
    EXPENSE_CATEGORIES,
)
from services.session_state import (
    compute_classifier_fingerprint,
    reset_review_session,
)
from services.invoice_repository import (
    save_pending,
    get_invoice,
    approve_invoice,
    reject_invoice,
    mark_paid,
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_REJECTED,
    MissingReviewerError,
    MissingReviewNoteError,
    InvalidStateTransition,
    InvalidPaymentState,
    InvoiceRepositoryError,
    NotFoundError,
)

from services.invoice_tracker import (
    search_tracker as _tracker_search,
    get_tracker_detail as _tracker_get_detail,
    update_pending_invoice_fields as _tracker_update_fields,
    ALL_DUE_STATES as _ALL_DUE_STATES,
    ALL_REVIEW_FILTERS as _ALL_REVIEW_FILTERS,
    ALL_PAYMENT_FILTERS as _ALL_PAYMENT_FILTERS,
    REVIEW_FILTER_ALL as _REVIEW_FILTER_ALL,
    PAYMENT_FILTER_ALL as _PAYMENT_FILTER_ALL,
    DUE_STATE_ALL as _DUE_STATE_ALL,
    CATEGORY_FILTER_ALL as _CATEGORY_FILTER_ALL,
    EDITABLE_FIELDS as _EDITABLE_FIELDS,
    InvoiceNotEditableError as _InvoiceNotEditableError,
    InvalidEditFieldError as _InvalidEditFieldError,
    InvalidFilterValueError as _InvalidFilterValueError,
    InvalidAmountError as _InvalidAmountError,
)

# Batch 4 - bookkeeping CSV export. Reuses the same filter SQL.
from services.csv_export import (
    build_tracker_csv,
    assert_no_sensitive_columns,
)

# Batch 4 - demo environment reset / seed (explicit, isolated).
from services.demo_reset import (
    DEFAULT_DEMO_DB_PATH,
    DEFAULT_DEMO_DOCS_DIR,
    DemoEnvironmentError,
    clear_demo_data,
    demo_db_summary,
    reset_demo_db,
    resolve_demo_db_path,
    seed_demo_invoices,
)

from services.document_storage import get_document_abs_path as _doc_abs_path


from services.batch_intake import (
    BatchIntakeFile,
    BATCH_MAX_PDF_BYTES,
    process_pdf_batch,
    queue_row_from_item,
)
from services.queue_status import (
    ALL_QUEUE_STATUSES,
    QUEUE_STATUS_FAILED,
    QUEUE_STATUS_NEEDS_REVIEW,
    QUEUE_STATUS_POSSIBLE_DUPLICATE,
    QUEUE_STATUS_READY,
    queue_status_priority,
)

st.set_page_config(
    page_title="OilOps AI",
    page_icon=":oil_drum:",
    layout="wide",
    initial_sidebar_state="expanded",
)

PAGES = (
    "Dashboard",
    "Process Invoice",
    "Batch Intake",
    "Invoice Tracker",
    "Weekly Summary",
    "About / Privacy",
)


def _sidebar_nav() -> str:
    with st.sidebar:
        st.markdown("## OilOps AI")
        st.caption("Office Operations Assistant")
        try:
            _active_db = resolve_db_path()
            _is_demo_mode = _active_db.name.lower() == "demo_oilops.db"
            if _is_demo_mode:
                st.warning("DEMO MODE — Synthetic Data Only")
        except Exception:
            pass
        st.markdown("---")
        choice = st.radio(
            "Navigation",
            options=list(PAGES),
            index=0,
            label_visibility="collapsed",
            key="oilops_nav_page",
        )
        st.markdown("---")
        st.caption(
            "Synthetic data only. Never upload real vendor "
            "documents to this development build."
        )
    return choice


def _fmt_money(amounts):
    if not amounts:
        return "-"
    return " / ".join(
        f"{ccy} {v:,.2f}" for ccy, v in sorted(amounts.items())
    )


def _kpi_card(label, value, help_text=None):
    st.metric(label=label, value=value, help=(help_text or None))


# ===========================================================================
# Dashboard page
# ===========================================================================
def render_dashboard() -> None:
    st.markdown("# OilOps AI")
    st.caption(
        "Office Operations Assistant. Invoice processing, "
        "review and payment tracking for oil & gas operations."
    )
    st.markdown("---")
    try:
        _conn = get_connection(None)
        try:
            _dash = compute_dashboard(_conn, date.today())
        finally:
            _conn.close()
    except Exception as _exc:
        st.error("Could not load dashboard: " + str(_exc))
        return
    _kpis = _dash.kpis
    st.markdown("### Key Numbers")
    _c1, _c2, _c3, _c4 = st.columns(4)
    with _c1:
        st.metric(label="Total Invoices", value=_kpis.total_invoices)
    with _c2:
        st.metric(
            label="Awaiting Human Review", value=_kpis.pending_review,
            help="Invoices awaiting human review.",
        )
    with _c3:
        st.metric(
            label="Due Soon", value=_kpis.due_this_week,
            help="Invoices due within the next 7 days.",
        )
    with _c4:
        st.metric(
            label="Overdue",
            value=_kpis.overdue,
            help="Past-due, not yet recorded as paid.",
        )
    _c5, _c6 = st.columns(2)
    with _c5:
        st.metric(
            label="Possible Duplicates",
            value=_kpis.possible_duplicate_groups,
            help="Invoice groups with matching vendor / invoice number / amount.",
        )
    with _c6:
        st.metric(
            label="Outstanding Amount",
            value=_fmt_money(_kpis.outstanding_amount.amounts),
            help="Approved but not yet recorded as paid.",
        )
    st.markdown("---")
    st.markdown("### Needs Attention")
    st.caption("Priority: Overdue > Due Soon > Pending > Duplicate.")
    if not _dash.needs_attention:
        st.info("No invoices need attention right now.")
    else:
        _attn_rows = []
        for _item in _dash.needs_attention:
            _attn_rows.append({
                "ID": _item.invoice_id,
                "Vendor": _item.vendor_name or "?",
                "Invoice #": _item.invoice_number or "?",
                "Due Date": _item.due_date or "?",
                "Total": float(_item.total_amount)
                if _item.total_amount is not None else None,
                "Currency": _item.currency or "",
                "Status": _item.status,
                "Payment": _item.payment_state_marker or "-",
                "Reasons": ", ".join(_item.reasons) if _item.reasons else "-",
            })
        st.dataframe(
            _attn_rows, hide_index=True, use_container_width=True,
            column_config={
                "Total": st.column_config.NumberColumn(format="%.2f"),
            },
        )
    st.markdown("### Approved Spend by Category")
    if not _dash.spend_by_category:
        st.info("No approved spend yet.")
    else:
        _cat_df = [
            {
                "Category": _c.category,
                "Currency": _c.currency,
                "Invoice Count": _c.invoice_count,
                "Total Amount": float(_c.total_amount),
            }
            for _c in _dash.spend_by_category
        ]
        st.dataframe(
            _cat_df, hide_index=True, use_container_width=True,
            column_config={
                "Total Amount": st.column_config.NumberColumn(format="%.2f"),
            },
        )
        st.caption(
            "Totals are kept separate per currency. "
            "CAD and USD are never combined."
        )
    st.markdown("### Top Vendors")
    st.caption("Approved spend, top 5 by total.")
    if not _dash.spend_by_vendor:
        st.info("No approved vendor spend yet.")
    else:
        _vendor_df = [
            {
                "Vendor": _v.vendor_name,
                "Currency": _v.currency,
                "Invoice Count": _v.invoice_count,
                "Total Amount": float(_v.total_amount),
            }
            for _v in _dash.spend_by_vendor
        ]
        st.dataframe(
            _vendor_df, hide_index=True, use_container_width=True,
            column_config={
                "Total Amount": st.column_config.NumberColumn(format="%.2f"),
            },
        )
        st.caption(
            "Totals kept per currency; CAD and USD are never combined."
        )
    st.markdown("### Recent Invoices")
    st.caption("Latest 10 invoices across all statuses.")
    if not _dash.recent_invoices:
        st.info("No invoices yet.")
    else:
        _recent_rows = []
        for _r in _dash.recent_invoices:
            _recent_rows.append({
                "ID": _r.id,
                "Vendor": _r.vendor_name or "?",
                "Invoice #": _r.invoice_number or "?",
                "Category": _r.category or "?",
                "Status": _r.status,
                "Payment": _r.payment_state_marker or "-",
                "Total": float(_r.total_amount)
                if _r.total_amount is not None else None,
                "Currency": _r.currency or "",
                "Created": str(_r.created_at or ""),
            })
        st.dataframe(
            _recent_rows, hide_index=True, use_container_width=True,
            column_config={
                "Total": st.column_config.NumberColumn(format="%.2f"),
            },
        )

# ===========================================================================
# Process Invoice page - Step 1 Upload
# ===========================================================================
def _render_pi_step1_upload():
    st.markdown("## 1. Upload Invoice")
    if st.button("Clear current review", key="pi_reset_session_btn"):
        reset_review_session(st.session_state)
        st.session_state.pop("uploaded_file_bytes", None)
        st.success("Review session cleared.")
    uploaded = st.file_uploader(
        "Choose an invoice PDF",
        type=["pdf"],
        key="pi_pdf_uploader",
    )
    if uploaded is not None:
        _raw_bytes = uploaded.getvalue()
        _prev = st.session_state.get("uploaded_file_bytes")
        if _prev != _raw_bytes:
            reset_review_session(st.session_state)
            st.session_state["uploaded_file_bytes"] = _raw_bytes
            st.session_state["parsed_invoice"] = parse_pdf(
                _raw_bytes, filename=uploaded.name
            )
        parsed = st.session_state["parsed_invoice"]
        st.write("**Filename:**", parsed.filename)
        st.write("**Pages:**", parsed.page_count)
        st.write("**Parse status:**", parsed.parsing_status)
        if parsed.error:
            st.error(parsed.error)
        else:
            with st.expander("Parsed text preview", expanded=False):
                st.text_area(
                    "Parsed text preview",
                    value=preview_text(parsed.raw_text),
                    height=180,
                    key="pi_parsed_text_preview",
                    label_visibility="collapsed",
                )


# ===========================================================================
# Process Invoice page - Step 2 Extracted Information
# ===========================================================================
def _render_pi_step2_extraction(parsed_inv):
    st.markdown("## 2. Extracted Information")
    if parsed_inv is None or parsed_inv.parsing_status != "ok":
        st.info("Upload a PDF first to extract structured fields.")
        return
    if st.session_state.get("extraction") is None:
        st.session_state["extraction"] = extract_invoice(
            parsed_inv.raw_text
        )
    ext_state = st.session_state["extraction"]
    with st.form("pi_step2_edit_form", clear_on_submit=False):
        col1, col2 = st.columns(2)
        with col1:
            _v = st.text_input(
                "Vendor name",
                value=ext_state.vendor_name or "",
                key="pi_ext_vendor",
            )
            _inv = st.text_input(
                "Invoice #",
                value=ext_state.invoice_number or "",
                key="pi_ext_invoice_no",
            )
            _idate = st.text_input(
                "Invoice date",
                value=ext_state.invoice_date or "",
                key="pi_ext_invoice_date",
            )
            _ddate = st.text_input(
                "Due date",
                value=ext_state.due_date or "",
                key="pi_ext_due_date",
            )
            _po = st.text_input(
                "PO #",
                value=ext_state.po_number or "",
                key="pi_ext_po",
            )
        with col2:
            _sub = st.text_input(
                "Subtotal",
                value=str(ext_state.subtotal)
                if ext_state.subtotal is not None else "",
                key="pi_ext_subtotal",
            )
            _gst = st.text_input(
                "GST/HST/Tax",
                value=str(ext_state.gst)
                if ext_state.gst is not None else "",
                key="pi_ext_gst",
            )
            _tot = st.text_input(
                "Total amount",
                value=str(ext_state.total_amount)
                if ext_state.total_amount is not None else "",
                key="pi_ext_total",
            )
            _cur = st.text_input(
                "Currency",
                value=ext_state.currency or "",
                key="pi_ext_currency",
            )
            _desc = st.text_area(
                "Description",
                value=ext_state.description or "",
                key="pi_ext_desc",
                height=80,
            )
        submitted = st.form_submit_button("Apply corrections")
        if submitted:
            ext_state.vendor_name = _v or None
            ext_state.invoice_number = _inv or None
            ext_state.invoice_date = _idate or None
            ext_state.due_date = _ddate or None
            ext_state.po_number = _po or None
            ext_state.description = _desc or None
            from decimal import Decimal as _D
            def _money(s):
                s = (s or "").strip().replace(",", "")
                if not s:
                    return None
                try:
                    return float(_D(s))
                except Exception:
                    return None
            ext_state.subtotal = _money(_sub)
            ext_state.gst = _money(_gst)
            ext_state.total_amount = _money(_tot)
            ext_state.currency = _cur or None
            st.session_state["reviewed_extraction"] = ext_state
            st.success("Corrections applied.")

            st.session_state["reviewed_extraction"] = ext_state
            st.success("Corrections applied.")


# ===========================================================================
# Process Invoice page - Step 3 Validation & Privacy
# ===========================================================================
def _render_pi_step3_validation(parsed_inv):
    st.markdown("## 3. Validation & Privacy")
    ext_state = (
        st.session_state.get("reviewed_extraction")
        or st.session_state.get("extraction")
    )
    if ext_state is None:
        st.info("Structured extraction is required first.")
        return
    # Privacy: redaction preview of sensitive fields
    try:
        _redacted = redact_text(parsed_inv.raw_text) \
            if parsed_inv is not None and parsed_inv.parsing_status == "ok" \
            else ""
        if _redacted:
            with st.expander(
                "Redacted preview (sensitive fields removed)",
                expanded=False,
            ):
                st.text_area(
                    "Redacted text",
                    value=_redacted,
                    height=180,
                    key="pi_redacted_preview",
                    label_visibility="collapsed",
                )
    except Exception:
        pass
    if st.button("Run validation", key="pi_run_validation_btn"):
        try:
            _vconn = get_connection(None)
            try:
                _vresult = check_invoice(
                    ext_state,
                    db_conn=_vconn,
                    reference_date=date.today(),
                )
            finally:
                _vconn.close()
            st.session_state["validation_result"] = _vresult
        except Exception as _exc:
            st.session_state["validation_result"] = None
            st.error("Validation error: " + str(_exc))
    _v = st.session_state.get("validation_result")
    if _v is not None:
        st.code(format_validation_summary(_v), language="text")
        for _iss in _v.issues:
            if _iss.severity == "error":
                st.error(_iss.message)
            elif _iss.severity == "warning":
                st.warning(_iss.message)
            else:
                st.info(_iss.message)


# ===========================================================================
# Process Invoice page - Step 4 AI Classification
# ===========================================================================
def _render_pi_step4_classification(parsed_inv):  # noqa: ARG001
    st.markdown("## 4. AI Classification")
    ext_state = (
        st.session_state.get("reviewed_extraction")
        or st.session_state.get("extraction")
    )
    if ext_state is None:
        st.info("Structured extraction is required first.")
        return
    _current_fp = compute_classifier_fingerprint(ext_state)
    _cached_fp = st.session_state.get("classification_fingerprint")
    if _cached_fp and _cached_fp != _current_fp:
        st.session_state.pop("classification_result", None)
        st.session_state.pop("classification_fingerprint", None)
        st.session_state.pop("classification_source_label", None)
        st.warning(
            "Extraction changed - previous AI classification cleared. "
            "Click 'Run AI classification' again to refresh."
        )
    _cached = st.session_state.get("classification_result")
    if _cached is not None:
        st.write(
            "Last classification: ",
            format_classification_summary(_cached),
        )
        if getattr(_cached, "is_low_confidence", False):
            st.warning("Low confidence - please review manually.")
    if st.button("Run AI classification", key="pi_run_classify_btn"):
        _api_key_present = is_api_key_configured()
        with st.spinner("Classifying invoice..."):
            try:
                _result = classify_expense(
                    ext_state, enable_ai=_api_key_present
                )
                st.session_state["classification_result"] = _result
                st.session_state["classification_fingerprint"] = _current_fp
                st.session_state["classification_source_label"] = _result.source
                st.write(
                    "Result: ",
                    format_classification_summary(_result),
                )
            except Exception as _exc:
                st.error("Classification failed: " + str(_exc))
    _cached = st.session_state.get("classification_result")
    _suggested = (
        _cached.category if _cached is not None else CATEGORY_OTHER
    )
    _suggested_idx = (
        list(EXPENSE_CATEGORIES).index(_suggested)
        if _suggested in EXPENSE_CATEGORIES
        else list(EXPENSE_CATEGORIES).index(CATEGORY_OTHER)
    )
    _reviewed_cat = st.selectbox(
        "Reviewed expense category",
        options=list(EXPENSE_CATEGORIES),
        index=_suggested_idx,
        key="pi_reviewed_category_select",
        help="Human-confirmed category.",
    )
    st.session_state["reviewed_category"] = _reviewed_cat

    st.session_state["reviewed_category"] = _reviewed_cat


# ===========================================================================
# Process Invoice page - Step 5 Human Review
# ===========================================================================
def _render_pi_step5_review(parsed_inv):
    st.markdown("## 5. Human Review")
    st.caption(
        "These actions record a human decision only. AI does "
        "not approve or reject."
    )
    reviewed_extraction = (
        st.session_state.get("reviewed_extraction")
        or st.session_state.get("extraction")
    )
    if reviewed_extraction is None:
        st.info(
            "Upload a PDF and complete the earlier steps before "
            "saving the invoice as Pending."
        )
        return
    reviewer_name = st.text_input(
        "Reviewer name",
        value=st.session_state.get("reviewer_name", ""),
        key="pi_reviewer_name_input",
        placeholder="e.g. Alice (Accounts Payable)",
    )
    validation = st.session_state.get("validation_result")
    can_save = False
    ack = False
    if validation is not None:
        st.markdown("**Validation**")
        st.code(
            format_validation_summary(validation), language="text"
        )
        errors = [
            i for i in validation.issues if i.severity == "error"
        ]
        warnings = [
            i for i in validation.issues if i.severity == "warning"
        ]
        for e in errors:
            st.error(e.message)
        if warnings:
            with st.expander(
                "Warnings (" + str(len(warnings)) + ")",
                expanded=False,
            ):
                for w in warnings:
                    st.warning(w.message)
        dup_issues = [
            i for i in validation.issues
            if i.code == CODE_POSSIBLE_DUPLICATE
        ]
        if dup_issues:
            dup_msg = " ".join(i.message for i in dup_issues)
            st.warning(
                "Possible duplicate detected. " + dup_msg
                + " Saving is still allowed but you must explicitly "
                "acknowledge below."
            )
            ack = st.checkbox(
                "I reviewed the possible duplicate warning and "
                "still want to save this invoice.",
                value=False,
                key="pi_ack_duplicate",
            )
        else:
            ack = True
        blocker_errors = [
            e for e in errors if e.code != CODE_AMOUNT_MISMATCH
        ]
        for e in blocker_errors:
            st.error(
                "Blocking validation error: " + e.message
                + " - this invoice cannot be saved."
            )
        can_save = not blocker_errors and bool(ack)
    else:
        st.info("Run validation first.")
    reviewed_category = st.session_state.get("reviewed_category")
    classification = st.session_state.get("classification_result")
    if st.button(
        "Save as Pending",
        key="pi_save_btn",
        disabled=not can_save,
    ):
        try:
            with transaction(None) as _tconn:
                _saved_id = save_pending(
                    _tconn,
                    reviewed_extraction,
                    source_filename=(
                        parsed_inv.filename
                        if parsed_inv is not None
                        else None
                    ),
                    expense_category=reviewed_category,
                    classification_source=(
                        classification.source
                        if classification is not None
                        else None
                    ),
                    classification=classification,
                    validation=validation,
                    reviewer=reviewer_name,
                )
            st.session_state["current_invoice_id"] = _saved_id
            st.session_state["current_invoice_status"] = STATUS_PENDING
            st.session_state[
                "current_invoice_payment_state"
            ] = "Not ready"
            st.success(
                "Saved as Pending. Invoice ID: " + str(_saved_id)
            )
        except Exception as _exc:
            st.error("Save failed: " + str(_exc))

            st.success(
                "Saved as Pending. Invoice ID: " + str(_saved_id)
            )
        except Exception as _exc:
            st.error("Save failed: " + str(_exc))
    saved_id = st.session_state.get("current_invoice_id")
    if saved_id is not None:
        st.markdown("---")
        st.markdown("### Human approval")
        current_status = st.session_state.get(
            "current_invoice_status", "pending"
        )
        if current_status == STATUS_PENDING:
            reject_note = st.text_area(
                "Rejection note (required if rejecting)",
                value=st.session_state.get("review_note", ""),
                key="pi_review_note_input",
                placeholder="e.g. Wrong vendor, missing PO.",
            )
            col_a, col_b = st.columns(2)
            with col_a:
                if st.button(
                    "Approve invoice",
                    key="pi_approve_btn",
                    disabled=not (reviewer_name or "").strip(),
                ):
                    try:
                        with transaction(None) as _tconn:
                            _saved = approve_invoice(
                                _tconn, saved_id,
                                reviewer=reviewer_name,
                                review_note=None,
                            )
                            st.session_state[
                                "current_invoice_status"
                            ] = _saved.status
                            st.session_state[
                                "current_invoice_payment_state"
                            ] = _saved.payment_state
                        st.success(
                            "Human-approved. Invoice "
                            + str(saved_id)
                            + " status: approved. Payment state: "
                            + _saved.payment_state + "."
                        )
                    except (
                        MissingReviewerError,
                        InvalidStateTransition,
                        NotFoundError,
                    ) as exc:
                        st.error("Approval failed: " + str(exc))
            with col_b:
                if st.button(
                    "Reject invoice",
                    key="pi_reject_btn",
                    disabled=not (
                        (reviewer_name or "").strip()
                        and (reject_note or "").strip()
                    ),
                ):
                    try:
                        with transaction(None) as _tconn:
                            _saved = reject_invoice(
                                _tconn, saved_id,
                                reviewer=reviewer_name,
                                review_note=reject_note,
                            )
                            st.session_state[
                                "current_invoice_status"
                            ] = _saved.status
                            st.session_state[
                                "current_invoice_payment_state"
                            ] = _saved.payment_state
                        st.success(
                            "Human-rejected. Invoice "
                            + str(saved_id)
                            + " status: rejected."
                        )
                    except (
                        MissingReviewerError,
                        MissingReviewNoteError,
                        InvalidStateTransition,
                        NotFoundError,
                    ) as exc:
                        st.error("Rejection failed: " + str(exc))
        else:
            st.info(
                "Current status: " + current_status
                + ". Approval / rejection buttons are disabled "
                "because the invoice is no longer pending."
            )
        payment_state = st.session_state.get(
            "current_invoice_payment_state"
        )
        is_paid = payment_state == "Paid"
        if current_status == STATUS_APPROVED and not is_paid:
            st.markdown("---")
            st.markdown("### Mark as Paid")
            st.caption(
                "This records that payment was completed outside "
                "OilOps AI. It does not send or initiate payment."
            )
            import datetime as _dt
            paid_at_input = st.date_input(
                "Payment date",
                value=_dt.date.today(),
                key="pi_paid_at_input",
            )
            payment_note_input = st.text_input(
                "Payment note (optional)",
                value="",
                key="pi_payment_note_input",
                placeholder="e.g. Wire ref WIRE-2026-0922-001",
            )
            if st.button("Mark as Paid", key="pi_mark_paid_btn"):
                try:
                    with transaction(None) as _tconn:
                        _saved = mark_paid(
                            _tconn, saved_id,
                            paid_at=paid_at_input.isoformat(),
                            payment_note=(payment_note_input or None),
                        )
                        st.session_state[
                            "current_invoice_status"
                        ] = _saved.status
                        st.session_state[
                            "current_invoice_payment_state"
                        ] = _saved.payment_state
                    st.success(
                        "Recorded as paid. Invoice "
                        + str(saved_id)
                        + " paid_at: " + str(_saved.paid_at)
                        + ". Payment State: "
                        + _saved.payment_state + "."
                    )
                except (InvalidPaymentState, NotFoundError) as exc:
                    st.error("Could not mark paid: " + str(exc))
        elif current_status == STATUS_APPROVED and is_paid:
            st.markdown("---")
            st.markdown("### Payment recorded")
            st.success(
                "This invoice is already recorded as paid. "
                "OilOps AI does not execute payments."
            )


# ===========================================================================
# Batch Invoice Intake page (Batch 2)
# ===========================================================================
def _bi_reset_queue_state() -> None:
    """Clear every Batch Intake artefact from session_state."""
    for key in (
        "bi_uploaded_files",
        "bi_queue_results",
        "bi_last_batch_summary",
        "bi_last_processed_at",
    ):
        try:
            st.session_state.pop(key, None)
        except Exception:
            pass


def _bi_render_queue_table(rows):
    """Render the queue rows as a Streamlit table."""
    if not rows:
        st.info("No items in the queue yet. Upload PDFs and click Process batch.")
        return
    rows_sorted = sorted(
        rows,
        key=lambda r: (queue_status_priority(r["status"]), r["filename"]),
    )
    table_rows = []
    for r in rows_sorted:
        validation = r.get("validation") or {}
        issues = validation.get("issues") or []
        if issues:
            codes = sorted({i.get("code") for i in issues if i.get("code")})
            validation_summary = ", ".join(codes)
        else:
            validation_summary = "clean"
        amount = r.get("total") or "?"
        currency = r.get("currency") or "?"
        table_rows.append({
            "Status": str(r["status"]),
            "Vendor": r.get("vendor") or "?",
            "Invoice #": r.get("invoice_number") or "?",
            "Total": str(amount),
            "Currency": str(currency),
            "AI Category": r.get("classification") or "(none)",
            "Issues": validation_summary,
        })
    st.dataframe(
        table_rows, use_container_width=True, hide_index=True,
        column_order=["Status", "Vendor", "Invoice #", "Total", "Currency", "AI Category", "Issues"],
    )


def _bi_render_summary(summary):
    """Render the batch summary card (counts per status)."""
    if not summary:
        return
    total = sum(int(summary.get(status, 0)) for status in ALL_QUEUE_STATUSES)
    st.markdown("### Batch Summary")
    labels = (
        ("Files Processed", total),
        ("Ready for Review", int(summary.get(QUEUE_STATUS_READY, 0))),
        ("Needs Review", int(summary.get(QUEUE_STATUS_NEEDS_REVIEW, 0))),
        ("Possible Duplicate", int(summary.get(QUEUE_STATUS_POSSIBLE_DUPLICATE, 0))),
        ("Failed", int(summary.get(QUEUE_STATUS_FAILED, 0))),
    )
    cols = st.columns(len(labels))
    for col, (label, count) in zip(cols, labels):
        with col:
            st.metric(label=label, value=count)


def _bi_render_queue_detail(rows):
    """Render an expandable detail panel for each queue row."""
    if not rows:
        return
    st.markdown("### Item details")
    rows_sorted = sorted(
        rows,
        key=lambda r: (queue_status_priority(r["status"]), r["filename"]),
    )
    for r in rows_sorted:
        validation = r.get("validation") or {}
        issues = validation.get("issues") or []
        dup_ids = r.get("duplicate_existing_ids") or []
        header = (
            (r.get("status") or "?")
            + " :: "
            + (r.get("filename") or "?")
        )
        with st.expander(header, expanded=False):
            st.write(
                "Vendor:", r.get("vendor") or "?",
                " | Invoice #:", r.get("invoice_number") or "?",
            )
            st.write(
                "Total:", str(r.get("total") or "?"),
                str(r.get("currency") or "?"),
                " | Category:", r.get("classification") or "(none)",
                "(" + str(r.get("classification_source") or "(none)") + ")",
            )
            st.write(
                "Invoice id:",
                r.get("invoice_id") if r.get("invoice_id") is not None else "(not saved)",
                " | Document path:", r.get("document_path") or "(not stored)",
            )
            if r.get("is_possible_duplicate"):
                st.warning(
                    "Possible duplicate. Matches existing invoice id(s): "
                    + (", ".join(str(x) for x in dup_ids) if dup_ids else "(unknown)")
                )
            if issues:
                st.markdown("**Validation issues**")
                for issue in issues:
                    sev = str(issue.get("severity") or "info")
                    code = str(issue.get("code") or "")
                    msg = str(issue.get("message") or "")
                    st.write("- [" + sev + "] " + code + ": " + msg)
            else:
                st.write("Validation: clean.")
            if r.get("error"):
                st.error("Pipeline error: " + str(r["error"]))


def _get_db_connection_safe():
    """Open the live DB connection the same way other pages do."""
    return get_connection(None)



def render_batch_intake() -> None:
    """Batch 2 - Invoice Intake Queue page."""
    st.markdown("# Batch Intake")
    st.caption(
        "Upload multiple invoice PDFs at once. Each file runs through "
        "the same parsing -> extraction -> privacy -> validation -> AI "
        "classification pipeline as Process Invoice, but failures are "
        "isolated per file and every item ends up in the Queue below."
    )
    uploaded = st.file_uploader(
        "Choose one or more invoice PDFs",
        type=["pdf"],
        accept_multiple_files=True,
        key="bi_pdf_uploader",
        help=(
            "You can drop in many PDFs at once. Original bytes stay "
            "on this machine and are stored under data/documents/ "
            "next to the existing Batch 1 layout. Only the AI-safe "
            "payload is sent to the classifier."
        ),
    )
    if uploaded:
        files = []
        for u in uploaded:
            try:
                raw = u.getvalue()
            except Exception:
                raw = b""
            files.append((u.name, raw))
        st.session_state["bi_uploaded_files"] = files
        st.write("Selected files:", len(files))


    files_state = st.session_state.get("bi_uploaded_files") or []
    if not files_state:
        st.info(
            "Drop one or more PDFs above to start. Synthetic Data Only — "
            "do not upload real vendor documents."
        )
        return


    oversized = [
        name for (name, raw) in files_state
        if isinstance(raw, (bytes, bytearray)) and len(raw) > BATCH_MAX_PDF_BYTES
    ]
    if oversized:
        st.error(
            "These files exceed the "
            + str(BATCH_MAX_PDF_BYTES // (1024 * 1024))
            + " MB per-file cap and will be marked Failed: "
            + ", ".join(oversized)
        )


    action_cols = st.columns([1, 1, 4])
    with action_cols[0]:
        process_clicked = st.button(
            "Process batch",
            type="primary",
            key="bi_process_btn",
            disabled=not files_state,
        )
    with action_cols[1]:
        clear_clicked = st.button(
            "Clear batch",
            key="bi_clear_btn",
        )
    if clear_clicked:
        _bi_reset_queue_state()
        st.rerun()


    if process_clicked:
        batch_files = [
            BatchIntakeFile(filename=name, raw_bytes=raw)
            for (name, raw) in files_state
        ]
        try:
            conn = _get_db_connection_safe()
        except Exception as exc:
            st.error("Cannot open the database: " + str(exc))
            return
        try:
            items = process_pdf_batch(batch_files, conn=conn)
            # Batch 2 requirement: duplicate rows MUST still be saved
            # (flagged as Possible Duplicate). The reviewer then
            # approves/rejects from Process Invoice. Auto-approve
            # and auto-pay are still off - all inserted rows keep
            # status='pending' until a human acts. Commit so the
            # pending rows + stored PDFs survive across sessions.
            conn.commit()
        finally:
            try:
                conn.close()
            except Exception:
                pass
        rows = [queue_row_from_item(it) for it in items]
        st.session_state["bi_queue_results"] = rows
        st.session_state["bi_last_batch_summary"] = {
            status: sum(1 for r in rows if r["status"] == status)
            for status in ALL_QUEUE_STATUSES
        }
        st.session_state["bi_last_processed_at"] = (
            datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )
        st.rerun()


    rows = st.session_state.get("bi_queue_results")
    summary = st.session_state.get("bi_last_batch_summary")
    processed_at = st.session_state.get("bi_last_processed_at")
    if not rows:
        return
    st.markdown("### Intake Queue")
    st.caption(
        "Each row is a synthetic invoice that just went through the "
        "full Batch 1 pipeline. Status is computed deterministically "
        "from parse result + validation flags. Nothing here is "
        "auto-approved: review and decide from Process Invoice."
    )
    if processed_at:
        st.caption("Last processed: " + str(processed_at))
    _bi_render_summary(summary)
    _bi_render_queue_table(rows)
    _bi_render_queue_detail(rows)



# ===========================================================================
# Process Invoice page entry point
# ===========================================================================
def render_process_invoice() -> None:
    st.markdown("# Process Invoice")
    st.caption(
        "Upload a PDF, extract fields, run validation and AI "
        "classification, then record a human decision."
    )
    st.caption(
        "Sensitive payment information is removed before any "
        "optional AI processing."
    )
    st.markdown("---")
    parsed_inv = st.session_state.get("parsed_invoice")
    _render_pi_step1_upload()
    st.markdown("---")
    _render_pi_step2_extraction(parsed_inv)
    st.markdown("---")
    _render_pi_step3_validation(parsed_inv)
    st.markdown("---")
    _render_pi_step4_classification(parsed_inv)
    st.markdown("---")
    _render_pi_step5_review(parsed_inv)


# ===========================================================================
# Invoice Tracker page (Batch 3)
# ===========================================================================
def _tracker_categories(conn):
    try:
        cur = conn.execute(
            "SELECT DISTINCT expense_category FROM invoices "
            "WHERE expense_category IS NOT NULL AND TRIM(expense_category) != '' "
            "ORDER BY expense_category"
        )
        return [str(r["expense_category"]) for r in cur.fetchall()]
    except Exception:
        return []


def _tracker_render_detail(detail):
    inv = detail.invoice
    st.markdown(
        "### Invoice #" + str(inv.id)
        + " - " + str(inv.vendor_name or "(no vendor)")
    )
    _dl1, _dl2 = st.columns(2)
    with _dl1:
        st.markdown("**Vendor:** " + str(inv.vendor_name or "-"))
        st.markdown("**Invoice #:** " + str(inv.invoice_number or "-"))
        st.markdown("**Invoice Date:** " + str(inv.invoice_date or "-"))
        st.markdown("**Due Date:** " + str(inv.due_date or "-"))
        st.markdown("**PO:** " + str(inv.po_number or "-"))
        st.markdown("**Category:** " + str(inv.expense_category or "-"))
        st.markdown("**Description:** " + str(inv.description or "-"))
    with _dl2:
        st.markdown("**Subtotal:** "
                    + (("%.2f " % inv.subtotal) if inv.subtotal is not None else "- ")
                    + str(inv.currency or ""))
        st.markdown("**GST:** "
                    + (("%.2f " % inv.gst) if inv.gst is not None else "- ")
                    + str(inv.currency or ""))
        st.markdown("**Total:** "
                    + (("%.2f " % inv.total_amount) if inv.total_amount is not None else "- ")
                    + str(inv.currency or ""))
        st.markdown("**Review status:** " + str(inv.status))
        st.markdown("**Payment status:** " + inv.payment_state)
        st.markdown(
            "**Paid at:** "
            + (str(inv.paid_at) if inv.paid_at else "-")
        )

    st.markdown("#### Document traceability")
    if inv.document_path:
        st.markdown("**Original Document** — Stored locally")
        st.markdown("**Integrity** — Verified")
        try:
            _abs = _doc_abs_path(inv.document_path)
        except Exception:
            _abs = None
        if _abs is not None:
            try:
                with open(_abs, "rb") as _fh:
                    _pdf_bytes = _fh.read()
                st.download_button(
                    label="Open / download original PDF",
                    data=_pdf_bytes,
                    file_name=str(inv.document_path).split("__", 1)[-1],
                    mime="application/pdf",
                    key="tracker_open_pdf_" + str(inv.id),
                )
            except Exception as _exc:
                st.warning(
                    "Could not read the stored PDF file: " + str(_exc)
                )
            with st.expander("Technical details", expanded=False):
                st.markdown("- Local file: `" + str(_abs) + "`")
                st.markdown(
                    "- SHA-256: `" + str(inv.document_sha256 or "-") + "`"
                )
                st.markdown(
                    "- File size: " + str(inv.document_size_bytes or 0) + " bytes"
                )
        else:
            st.caption(
                "Original PDF was referenced but the file is "
                "missing on disk. This is non-fatal."
            )
    else:
        st.caption(
            "No original PDF stored for this invoice (legacy row)."
        )

    st.markdown("#### Validation warnings")
    if detail.validation is None:
        st.caption("No validation flags recorded for this invoice.")
    elif not detail.validation.has_issues:
        st.success(
            "No validation issues. ("
            + format_validation_summary(detail.validation) + ")"
        )
    else:
        for _issue in detail.validation.issues:
            _lbl = "[" + _issue.severity + "] " + _issue.code
            st.markdown("- " + _lbl + " - " + _issue.message)
        st.caption(format_validation_summary(detail.validation))

def _tracker_render_edit_panel(detail):
    inv = detail.invoice
    if inv.status != STATUS_PENDING:
        st.info(
            "This invoice is " + str(inv.status)
            + " and cannot be edited. Human Review already acted on it."
        )
        return
    st.markdown("---")
    st.markdown("#### Edit pending invoice")
    st.caption(
        "Edit business fields and re-run the deterministic validator. "
        "Approval / payment are still driven by Human Review buttons below."
    )
    _key_prefix = "tracker_edit_" + str(inv.id)
    with st.form(key=_key_prefix + "_form"):
        _e1, _e2, _e3 = st.columns(3)
        with _e1:
            _new_vendor = st.text_input(
                "Vendor", value=str(inv.vendor_name or ""),
                key=_key_prefix + "_vendor",
            )
            _new_inv_no = st.text_input(
                "Invoice #", value=str(inv.invoice_number or ""),
                key=_key_prefix + "_inv_no",
            )
            _new_inv_date = st.text_input(
                "Invoice Date (YYYY-MM-DD)",
                value=str(inv.invoice_date or ""),
                key=_key_prefix + "_inv_date",
            )
            _new_due_date = st.text_input(
                "Due Date (YYYY-MM-DD)",
                value=str(inv.due_date or ""),
                key=_key_prefix + "_due_date",
            )
            _new_po = st.text_input(
                "PO", value=str(inv.po_number or ""),
                key=_key_prefix + "_po",
            )
        with _e2:
            _new_subtotal = st.text_input(
                "Subtotal",
                value=("" if inv.subtotal is None else ("%.2f" % inv.subtotal)),
                key=_key_prefix + "_subtotal",
            )
            _new_gst = st.text_input(
                "GST",
                value=("" if inv.gst is None else ("%.2f" % inv.gst)),
                key=_key_prefix + "_gst",
            )
            _new_total = st.text_input(
                "Total",
                value=("" if inv.total_amount is None else ("%.2f" % inv.total_amount)),
                key=_key_prefix + "_total",
            )
            _new_currency = st.text_input(
                "Currency", value=str(inv.currency or ""),
                key=_key_prefix + "_currency",
            )
        with _e3:
            _new_description = st.text_area(
                "Description",
                value=str(inv.description or ""),
                key=_key_prefix + "_description",
                height=120,
            )
        _submitted = st.form_submit_button("Save changes & revalidate")
    if not _submitted:
        return
    _edits = {}
    if _new_vendor != (inv.vendor_name or ""):
        _edits["vendor_name"] = _new_vendor
    if _new_inv_no != (inv.invoice_number or ""):
        _edits["invoice_number"] = _new_inv_no
    if _new_inv_date != (inv.invoice_date or ""):
        _edits["invoice_date"] = _new_inv_date
    if _new_due_date != (inv.due_date or ""):
        _edits["due_date"] = _new_due_date
    if _new_po != (inv.po_number or ""):
        _edits["po_number"] = _new_po
    if _new_currency != (inv.currency or ""):
        _edits["currency"] = _new_currency
    if _new_description != (inv.description or ""):
        _edits["description"] = _new_description
    if _new_subtotal != ("" if inv.subtotal is None else ("%.2f" % inv.subtotal)):
        _edits["subtotal"] = _new_subtotal
    if _new_gst != ("" if inv.gst is None else ("%.2f" % inv.gst)):
        _edits["gst"] = _new_gst
    if _new_total != ("" if inv.total_amount is None else ("%.2f" % inv.total_amount)):
        _edits["total_amount"] = _new_total
    if not _edits:
        st.info("No changes detected.")
        return
    try:
        _conn = get_connection(None)
        try:
            _new_detail = _tracker_update_fields(
                _conn, inv.id, _edits,
                reference_date=date.today(),
            )
            _conn.commit()
        finally:
            _conn.close()
    except _InvoiceNotEditableError as _exc:
        st.error(str(_exc))
        return
    except (_InvalidEditFieldError, _InvalidAmountError) as _exc:
        st.error("Edit rejected - " + str(_exc))
        return
    except Exception as _exc:
        st.error("Edit failed: " + str(_exc))
        return
    st.success("Saved. Validation re-run below.")
    _tracker_render_detail(_new_detail)
    _tracker_render_edit_panel(_new_detail)

def render_tracker() -> None:
    st.markdown("# Invoice Tracker")
    st.caption(
        "Search invoices by vendor / invoice # / PO. "
        "Filter by review status, payment status, category, "
        "currency, and due state."
    )
    st.markdown("---")

    try:
        _conn = get_connection(None)
        try:
            _categories = _tracker_categories(_conn)
        finally:
            _conn.close()
    except Exception as _exc:
        st.error("Could not open the database: " + str(_exc))
        return

    _search = st.text_input(
        "Search vendor / invoice # / PO",
        value="",
        key="tracker_search_input",
    )
    _f1, _f2, _f3, _f4, _f5 = st.columns(5)
    with _f1:
        _review_filter = st.selectbox(
            "Review status",
            options=list(_ALL_REVIEW_FILTERS),
            index=0,
            key="tracker_review_filter",
        )
    with _f2:
        _payment_filter = st.selectbox(
            "Payment status",
            options=list(_ALL_PAYMENT_FILTERS),
            index=0,
            key="tracker_payment_filter",
        )
    with _f3:
        _category_options = [_CATEGORY_FILTER_ALL] + list(_categories)
        _category_filter = st.selectbox(
            "Category",
            options=_category_options,
            index=0,
            key="tracker_category_filter",
        )
    with _f4:
        _currency_filter = st.selectbox(
            "Currency",
            options=["ALL", "CAD", "USD"],
            index=0,
            key="tracker_currency_filter",
        )
    with _f5:
        _due_state_filter = st.selectbox(
            "Due state",
            options=list(_ALL_DUE_STATES),
            index=0,
            key="tracker_due_state_filter",
        )

    try:
        _ref_date = date.today()
        _conn = get_connection(None)
        try:
            _rows = _tracker_search(
                _conn,
                search=_search,
                review_filter=_review_filter,
                payment_filter=_payment_filter,
                currency=_currency_filter,
                category=(_category_filter if _category_filter != _CATEGORY_FILTER_ALL else None),
                due_state=_due_state_filter,
                reference_date=_ref_date,
                limit=500,
            )
        finally:
            _conn.close()
    except _InvalidFilterValueError as _exc:
        st.error("Invalid filter: " + str(_exc))
        return
    except Exception as _exc:
        st.error("Could not load tracker: " + str(_exc))
        return

    # Batch 4 - Bookkeeping CSV Export.
    # Use the SAME filter state as the visible tracker table.
    _export_col, _ = st.columns([1, 5])
    with _export_col:
        _export_clicked = st.button(
            "Export current view as CSV",
            key="tracker_export_csv_btn",
            help="Download the invoices matching the current search "
                 + "and filters as a bookkeeping-ready CSV.",
        )
    if _export_clicked:
        try:
            _conn = get_connection(None)
            try:
                _csv_text = build_tracker_csv(
                    _conn,
                    search=_search,
                    review_filter=_review_filter,
                    payment_filter=_payment_filter,
                    currency=_currency_filter,
                    category=(
                        _category_filter
                        if _category_filter != _CATEGORY_FILTER_ALL
                        else None
                    ),
                    due_state=_due_state_filter,
                    reference_date=_ref_date,
                    limit=5000,
                )
            finally:
                _conn.close()
            assert_no_sensitive_columns(_csv_text)
        except Exception as _exc:
            st.error("CSV export failed: " + str(_exc))
        else:
            st.success(
                "Exporting " + str(len(_rows)) + " invoice(s)."
            )
            st.download_button(
                label="Download CSV",
                data=_csv_text,
                file_name="oilops_tracker_export.csv",
                mime="text/csv",
                key="tracker_export_csv_download",
            )

    st.caption(str(len(_rows)) + " invoice(s) match.")

    if not _rows:
        st.info("No invoices match the current search / filters.")
        return

    _table = []
    for _r in _rows:
        _table.append({
            "ID": _r.id,
            "Vendor": _r.vendor_name or "-",
            "Invoice #": _r.invoice_number or "-",
            "PO": _r.po_number or "-",
            "Due Date": _r.due_date or "-",
            "Total": float(_r.total_amount) if _r.total_amount is not None else None,
            "Currency": _r.currency or "-",
            "Category": _r.expense_category or "-",
            "Review": _r.status,
            "Payment": _r.payment_state,
        })
    st.dataframe(
        _table,
        hide_index=True,
        use_container_width=True,
        column_config={
            "Total": st.column_config.NumberColumn(format="%.2f"),
        },
    )

    _ids = [str(_r.id) for _r in _rows]
    _selected = st.selectbox(
        "Open invoice detail",
        options=["(none)"] + _ids,
        index=0,
        key="tracker_detail_select",
    )
    if _selected == "(none)":
        return
    try:
        _selected_id = int(_selected)
        _conn = get_connection(None)
        try:
            _detail = _tracker_get_detail(_conn, _selected_id)
        finally:
            _conn.close()
    except NotFoundError:
        st.error("Invoice not found.")
        return
    except Exception as _exc:
        st.error("Could not load detail: " + str(_exc))
        return

    _tracker_render_detail(_detail)
    _tracker_render_edit_panel(_detail)
    _tracker_render_review_actions(_detail)

def _tracker_render_review_actions(detail):
    st.markdown("---")
    st.markdown("#### Human review actions")
    st.caption(
        "Final approval and payment status remain under human control."
    )
    _saved = detail.invoice
    if _saved.status == STATUS_APPROVED:
        if _saved.paid_at:
            st.info("Already paid on " + str(_saved.paid_at) + ".")
        else:
            _pa = st.button(
                "Mark as paid (record only)",
                key="tracker_mark_paid_" + str(_saved.id),
            )
            if _pa:
                try:
                    _conn = get_connection(None)
                    try:
                        _now_iso = datetime.now().isoformat(sep=" ", timespec="seconds")
                        _saved2 = mark_paid(
                            _conn, _saved.id,
                            paid_at=_now_iso,
                            payment_note="Recorded from Tracker",
                        )
                        _conn.commit()
                    finally:
                        _conn.close()
                    st.success(
                        "Marked paid. payment_state = "
                        + _saved2.payment_state
                    )
                    st.rerun()
                except (InvalidPaymentState, NotFoundError) as _exc:
                    st.error(str(_exc))
    elif _saved.status == STATUS_REJECTED:
        st.caption("Rejected. Re-evaluate by editing + revalidating if needed.")
    else:
        _ra, _rr = st.columns(2)
        with _ra:
            _reviewer = st.text_input(
                "Reviewer name",
                key="tracker_reviewer_" + str(_saved.id),
            )
            _approve = st.button(
                "Approve",
                key="tracker_approve_" + str(_saved.id),
            )
        with _rr:
            _note = st.text_area(
                "Rejection note (required for reject)",
                key="tracker_reject_note_" + str(_saved.id),
                height=80,
            )
            _reject = st.button(
                "Reject",
                key="tracker_reject_" + str(_saved.id),
            )
        if _approve:
            try:
                _conn = get_connection(None)
                try:
                    _saved2 = approve_invoice(
                        _conn, _saved.id,
                        reviewer=_reviewer,
                    )
                    _conn.commit()
                finally:
                    _conn.close()
                st.success("Approved.")
                st.rerun()
            except (
                MissingReviewerError,
                InvalidStateTransition,
                NotFoundError,
            ) as _exc:
                st.error(str(_exc))
        if _reject:
            try:
                _conn = get_connection(None)
                try:
                    _saved2 = reject_invoice(
                        _conn, _saved.id,
                        reviewer=_reviewer,
                        review_note=_note,
                    )
                    _conn.commit()
                finally:
                    _conn.close()
                st.success("Rejected.")
                st.rerun()
            except (
                MissingReviewerError,
                MissingReviewNoteError,
                InvalidStateTransition,
                NotFoundError,
            ) as _exc:
                st.error(str(_exc))


# ===========================================================================
# Weekly Summary page
# ===========================================================================


# ===========================================================================
# Weekly Summary page
# ===========================================================================
def render_weekly_summary() -> None:
    st.markdown("# Weekly Summary")
    st.caption(
        "Deterministic weekly operations summary. CAD and USD "
        "are never combined."
    )
    st.markdown("---")
    _weekly_ref_date = st.date_input(
        "Reference date",
        value=date.today(),
        key="weekly_ref_date",
        help="Summary period ends on this date.",
    )
    try:
        _wconn = get_connection(None)
        try:
            _weekly_facts = compute_weekly_facts(
                _wconn, _weekly_ref_date
            )
        finally:
            _wconn.close()
    except Exception as exc:
        st.error("Could not load weekly summary: " + str(exc))
        return
    _wf = _weekly_facts
    st.markdown(
        "**Summary period:** "
        + _wf.period_start.isoformat()
        + " to "
        + _wf.period_end.isoformat()
        + "  (7 calendar days, inclusive)"
    )
    _r1c1, _r1c2, _r1c3, _r1c4 = st.columns(4)
    with _r1c1:
        st.metric(
            label="Recorded This Week",
            value=_wf.recorded_this_week_count,
        )
    with _r1c2:
        st.metric(
            label="Awaiting Human Review",
            value=_wf.pending_review_count,
        )
    with _r1c3:
        st.metric(
            label="Outstanding",
            value=_fmt_money(_wf.outstanding_amounts),
        )
    with _r1c4:
        st.metric(
            label="Paid This Week",
            value=_fmt_money(_wf.paid_this_week_amounts),
        )
    _r2c1, _r2c2, _r2c3, _r2c4 = st.columns(4)
    with _r2c1:
        st.metric(
            label="Due Next 7 Days",
            value=_wf.due_next_7_days_count,
        )
    with _r2c2:
        st.metric(
            label="Overdue",
            value=_wf.overdue_count,
        )
    with _r2c3:
        st.metric(
            label="Duplicate Groups",
            value=_wf.possible_duplicate_groups,
        )
    with _r2c4:
        st.metric(
            label="Attention Items",
            value=_wf.attention_count,
        )
    st.caption(
        "Recorded = invoices whose date(created_at) falls inside "
        "the period. Not the same as when the vendor actually "
        "sent the document."
    )
    st.caption(
        "Recorded as paid means the invoice was marked paid in "
        "OilOps AI. No external payment was executed."
    )
    st.markdown("### Spend by Category (approved)")
    if not _wf.approved_spend_by_category:
        st.info("No approved spend yet.")
    else:
        _cat_df = [
            {
                "Category": _wc.category,
                "Currency": _wc.currency,
                "Invoice Count": _wc.invoice_count,
                "Total Amount": float(_wc.total_amount),
            }
            for _wc in _wf.approved_spend_by_category
        ]
        st.dataframe(
            _cat_df,
            hide_index=True,
            use_container_width=True,
            column_config={
                "Total Amount": st.column_config.NumberColumn(format="%.2f"),
            },
        )
        st.caption(
            "Totals are kept separate per currency. "
            "CAD and USD are never combined."
        )
    if _wf.duplicate_groups:
        st.markdown("### Possible Duplicate Groups")
        _dg_rows = []
        for _d in _wf.duplicate_groups:
            _dg_rows.append({
                "Vendor": _d.vendor_name or "?",
                "Invoice #": _d.invoice_number or "?",
                "Total": float(_d.total_amount)
                if _d.total_amount is not None else None,
                "Count": _d.invoice_count,
            })
        st.dataframe(
            _dg_rows, hide_index=True, use_container_width=True,
            column_config={
                "Total": st.column_config.NumberColumn(format="%.2f"),
            },
        )
        st.caption(
            "Possible duplicate groups require human review. They "
            "are not automatically flagged as fraud."
        )

        st.caption(
            "Possible duplicate groups require human review. They "
            "are not automatically flagged as fraud."
        )
    # AI-assisted narrative
    st.markdown("### AI-assisted Narrative (optional)")
    _weekly_payload = build_weekly_summary_payload(_weekly_facts)
    assert_weekly_summary_payload_safe(_weekly_payload)
    _weekly_fingerprint = compute_weekly_payload_fingerprint(
        _weekly_payload
    )
    _api_configured = is_narrative_api_key_configured()
    if _api_configured:
        _ai_btn_label = "Generate AI weekly summary"
        _ai_help = "API key is configured. Click to call the provider."
    else:
        _ai_btn_label = "Generate AI weekly summary (deterministic fallback)"
        _ai_help = (
            "No API key is set. A deterministic fallback summary "
            "will be shown."
        )
    _cached = st.session_state.get("weekly_narrative_cache", {})
    _cached_entry = _cached.get(_weekly_fingerprint)
    if (
        isinstance(_cached_entry, dict)
        and _cached_entry.get("source") in ("llm", "mock")
    ):
        st.caption(
            "Cached narrative (source: "
            + str(_cached_entry.get("source"))
            + "). Click the button to refresh."
        )
    _clicked = st.button(
        _ai_btn_label,
        key="weekly_generate_narrative_btn",
        help=_ai_help,
    )
    if _clicked:
        if _api_configured:
            from services.weekly_summary import get_narrative_provider
            _prov = get_narrative_provider()
            _text, _source = generate_weekly_narrative(
                _weekly_facts, provider=_prov
            )
        else:
            _text, _source = generate_weekly_narrative(_weekly_facts)
        st.session_state["weekly_narrative_cache"] = {
            _weekly_fingerprint: {"text": _text, "source": _source},
        }
        st.session_state["weekly_narrative_source"] = _source
        st.session_state["weekly_narrative_text"] = _text
        st.rerun()
    _cached_text = st.session_state.get("weekly_narrative_text")
    if _cached_text:
        _cached_src = st.session_state.get(
            "weekly_narrative_source", "deterministic"
        )
        st.info(_cached_text)
        st.caption("Source: " + str(_cached_src))


# ===========================================================================
# About / Privacy page
# ===========================================================================
def render_about_privacy() -> None:
    st.markdown("# About / Privacy")
    st.caption(
        "What OilOps AI does and does not do."
    )
    st.markdown("---")
    st.markdown(
        "OilOps AI is an internal office-operations tool for "
        "processing, reviewing and tracking invoices in an oil "
        "& gas operations context."
    )
    st.markdown("### What OilOps AI does")
    st.markdown(
        "- **Local PDF parsing** — invoices are read on the "
        "local machine. Sensitive raw fields are stripped before "
        "any optional external call."
    )
    st.markdown(
        "- **Sensitive-data minimization** — bank account, "
        "routing, SWIFT / IBAN, email, phone, and payment "
        "instruction fields are never sent to an LLM provider."
    )
    st.markdown(
        "- **Human-controlled approval** — AI suggests a "
        "category and surfaces validation issues. The reviewer "
        "explicitly approves or rejects each invoice."
    )
    st.markdown(
        "- **Payment tracking only** — 'Mark as Paid' records "
        "the date a human confirms payment was completed "
        "outside OilOps AI."
    )
    st.markdown("### What OilOps AI does NOT do")
    st.markdown(
        "- **AI does not approve invoices.** Every approval "
        "action is a human button click."
    )
    st.markdown(
        "- **AI does not execute payments.** OilOps AI never "
        "calls a bank, ACH, EFT, wire, card, or payment API."
    )
    st.markdown(
        "- **AI does not persist sensitive raw payment fields.** "
        "The redacted preview is the only thing exposed to the "
        "optional LLM call."
    )
    st.markdown("### Data")
    st.markdown(
        "- **Synthetic demo data only** — all numbers shown in "
        "this build come from synthetic test invoices. Never "
        "upload real vendor documents to this development build."
    )
    st.markdown(
        "- **Local SQLite database** — `database/oilops.db` "
        "holds the persisted invoices. There is no network "
        "exposure."
    )
    with st.expander("Database status", expanded=False):
        try:
            _aconn = get_connection(None)
            try:
                st.write("Tables:", list_tables(_aconn))
                st.write("Indexes:", list_indexes(_aconn))
                st.write(
                    "Applied migration version:",
                    get_applied_version(_aconn),
                )
            finally:
                _aconn.close()
        except Exception as _exc:
            st.write("DB introspection error:", str(_exc))
    _render_demo_environment()


# ===========================================================================
# Batch 4 - Demo environment (explicit, isolated)
def _render_demo_environment() -> None:
    """Explicit demo reset / seed UI.

    Lives under the About / Privacy page. NEVER auto-invoked.
    User DB is at OILOPS_DB_PATH - it is NEVER touched.
    """
    st.markdown("### Demo environment")
    st.caption(
        "For the Synthetic Business E2E Test, demos and walk-throughs. "
        "The demo DB lives at a separate path and is never auto-reset. "
        "Pressing the button below explicitly destroys and re-creates the "
        "demo database only."
    )
    try:
        _demo_path = resolve_demo_db_path()
    except Exception as _exc:
        st.error("Cannot resolve demo DB path: " + str(_exc))
        return
    try:
        _summary = demo_db_summary()
        _demo_count = (
            int(_summary.get("count", 0))
            if _summary.get("exists")
            else 0
        )
    except Exception as _exc:
        st.error("Cannot introspect demo DB: " + str(_exc))
        _demo_count = None
    st.write(
        {
            "Demo DB path": str(_demo_path),
            "Demo docs dir": str(DEFAULT_DEMO_DOCS_DIR),
            "Status": "exists" if (_demo_count is not None) else "error",
            "Invoice count": _demo_count,
        }
    )
    st.caption(
        "User DB path (untouched): "
        + str(
            Path(__file__).resolve().parent
            / "database"
            / "oilops.db"
        )
    )
    _confirm = st.checkbox(
        "I understand this DELETES the demo database and re-creates it.",
        key="demo_reset_confirm",
    )
    _clicked = st.button(
        "Reset demo database and seed",
        key="demo_reset_btn",
        disabled=not _confirm,
    )
    if _clicked:
        try:
            _path = reset_demo_db()
            _sconn = get_connection(str(_path))
            try:
                _ids = seed_demo_invoices(_sconn)
                _sconn.commit()
            finally:
                _sconn.close()
            st.success(
                "Demo database reset. Seeded "
                + str(len(_ids))
                + " invoices."
            )
        except DemoEnvironmentError as _exc:
            st.error("Demo reset refused: " + str(_exc))
        except Exception as _exc:
            st.error("Demo reset failed: " + str(_exc))

    st.markdown("### Clear demo data")
    st.caption(
        "Clears demo invoice rows and demo documents only; the schema stays "
        "intact and no seed data is added."
    )
    _clear_confirm = st.checkbox(
        "I understand this deletes all demo invoice rows.",
        key="demo_clear_confirm",
    )
    if st.button(
        "Clear Demo Data",
        key="demo_clear_btn",
        disabled=not _clear_confirm,
    ):
        try:
            _remaining = clear_demo_data(db_path=str(resolve_db_path()))
            st.success("Demo data cleared. Invoice count: " + str(_remaining))
        except DemoEnvironmentError as _exc:
            st.error("Clear refused: " + str(_exc))
        except Exception as _exc:
            st.error("Clear failed: " + str(_exc))


# ===========================================================================
# Router
# ===========================================================================
            st.write("DB introspection error:", str(_exc))


# ===========================================================================
# Router
# ===========================================================================
def main() -> None:
    page = _sidebar_nav()
    if page == "Dashboard":
        render_dashboard()
    elif page == "Process Invoice":
        render_process_invoice()
    elif page == "Batch Intake":
        render_batch_intake()
    elif page == "Invoice Tracker":
        render_tracker()
    elif page == "Weekly Summary":
        render_weekly_summary()
    elif page == "About / Privacy":
        render_about_privacy()


main()
