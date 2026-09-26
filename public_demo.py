"""OilOps AI - public demo entrypoint for Streamlit Community Cloud.

DESIGN INTENT
-------------
This module is the *only* entrypoint exposed to public visitors on
share.streamlit.io. It is intentionally small and strict:

* Every visitor gets their own session-scoped SQLite database and
  document storage directory. Nothing is shared between sessions and
  nothing persists past a container restart.
* All AI / narrative features are forced into the deterministic mock
  path. The OPENAI_API_KEY environment variable is NEVER consulted,
  even if a malicious visitor manages to set it via st.secrets.
* File uploads from the public UI are disabled. Visitors can only
  re-run the per-file pipeline against the synthetic PDF fixtures
  already bundled in this repository (data/demo_documents/*.pdf).
* A persistent "Public Demo - Synthetic Data Only" banner is rendered
  on every page so visitors understand what they are looking at.

This file REUSES services/, database/, and tests/ without modifying
them. The local desktop app (app.py) is unchanged.
"""
from __future__ import annotations

import os
import sys
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import List

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import streamlit as st  # noqa: E402

st.set_page_config(
    page_title="OilOps AI - Public Demo",
    page_icon=":oil_drum:",
    layout="wide",
    initial_sidebar_state="expanded",
)

_PUBLIC_BANNER_HTML = (
    "<div role=\"alert\" style=\"background-color:#fff3cd;color:#664d03;"
    "border:1px solid #ffe69c;border-radius:6px;padding:12px 16px;"
    "margin:0 0 16px 0;font-size:15px;line-height:1.4;\">"
    "<strong>Public Demo - Synthetic Data Only.</strong> "
    "All vendor names, invoice numbers, and amounts shown here are "
    "<em>generated</em> for demonstration. No real vendor, no real "
    "bank account, and no real money is involved. AI features run in "
    "<strong>deterministic mock mode</strong> - no external API call "
    "is ever made. File uploads are disabled.</div>"
)
st.markdown(_PUBLIC_BANNER_HTML, unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Session-scoped storage helpers.
# ---------------------------------------------------------------------------

def _public_root() -> Path:
    """Root directory for all public-mode session storage."""
    root = Path(tempfile.gettempdir()) / "oilops_public_sessions"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _get_session_id() -> str:
    """Return a stable session id (uuid4 hex), creating one if needed."""
    sid = st.session_state.get("_public_session_id")
    if not sid:
        sid = uuid.uuid4().hex[:16]
        st.session_state["_public_session_id"] = sid
    return sid


def _session_dir() -> Path:
    sid = _get_session_id()
    d = _public_root() / sid
    (d / "docs").mkdir(parents=True, exist_ok=True)
    return d


def _session_db_path() -> Path:
    return _session_dir() / "demo.sqlite"


def _session_docs_dir() -> Path:
    return _session_dir() / "docs"


def _swap_docs_root_for_call(new_root: Path):
    """Temporarily point OILOPS_DOCS_DIR at new_root.

    Returns a token dict the caller MUST pass to _restore_docs_root
    in a finally block so the process-global env var is always
    restored, even on exception.
    """
    token = {
        "had_key": "OILOPS_DOCS_DIR" in os.environ,
        "old_value": os.environ.get("OILOPS_DOCS_DIR"),
    }
    os.environ["OILOPS_DOCS_DIR"] = str(new_root)
    return token


def _restore_docs_root(token: dict) -> None:
    if token["had_key"]:
        os.environ["OILOPS_DOCS_DIR"] = token["old_value"]
    else:
        os.environ.pop("OILOPS_DOCS_DIR", None)


# ---------------------------------------------------------------------------
# Force-disable any external AI provider. Even if OPENAI_API_KEY is
# somehow present in the environment, classify_expense() is always
# called with enable_ai=False and provider=MockProvider() below, so
# no live network call can ever occur in public mode.
# ---------------------------------------------------------------------------
os.environ.pop("OPENAI_API_KEY", None)
try:  # pragma: no cover - Streamlit secrets path
    if "OPENAI_API_KEY" in st.secrets:
        # Intentionally never copied into os.environ.
        pass
except Exception:
    pass


# ---------------------------------------------------------------------------
# Project imports (kept after set_page_config so the banner renders first
# and so any service import error does not blank the page silently).
# ---------------------------------------------------------------------------
from database.db import (  # noqa: E402
    init_db,
    transaction,
)
from services.weekly_summary import (  # noqa: E402
    build_deterministic_weekly_summary,
)
from services.dashboard_service import compute_dashboard  # noqa: E402
from services.invoice_parser import parse_pdf  # noqa: E402
from services.invoice_extractor import (  # noqa: E402
    extract_invoice,
    InvoiceExtraction,
)
from services.privacy_filter import redact_text  # noqa: E402
from services.invoice_checker import check_invoice  # noqa: E402
from services.ai_classifier import (  # noqa: E402
    classify_expense,
    MockProvider,
    SOURCE_MOCK,
    CATEGORY_OTHER,
)
from services.invoice_repository import (  # noqa: E402
    save_pending,
    approve_invoice,
    reject_invoice,
    mark_paid,
)
try:  # pragma: no cover - list_invoices may not exist in older builds
    from services.invoice_repository import list_invoices, get_invoice  # noqa: E402
except ImportError:
    list_invoices = None  # type: ignore[assignment]
    get_invoice = None  # type: ignore[assignment]

from services.document_storage import save_pdf  # noqa: E402
from services.csv_export import (  # noqa: E402
    build_tracker_csv,
    assert_no_sensitive_columns,
)


# ---------------------------------------------------------------------------
# Idempotent seeding + sample PDF list.
# ---------------------------------------------------------------------------
_DEMO_PDF_DIR = PROJECT_ROOT / "data" / "demo_documents"


def _list_synthetic_pdfs() -> List[Path]:
    """Return the committed synthetic PDFs (data/demo_documents/*.pdf)."""
    if not _DEMO_PDF_DIR.exists():
        return []
    return sorted(p for p in _DEMO_PDF_DIR.iterdir()
                  if p.suffix.lower() == ".pdf")


def _insert_seed_row(conn, *, vendor, inv_no, inv_date, due_date, po,
                    subtotal, gst, total, currency, description,
                    category) -> int:
    ext = InvoiceExtraction(
        vendor_name=vendor,
        invoice_number=inv_no,
        invoice_date=inv_date,
        due_date=due_date,
        po_number=po,
        description=description,
        subtotal=subtotal,
        gst=gst,
        total_amount=total,
        currency=currency,
        warnings=[],
    )
    return int(save_pending(conn, ext,
                            source_filename="public_demo_seed.pdf",
                            expense_category=category))


def _seed_session_if_empty() -> None:
    """Insert a few representative rows so first-time visitors see KPIs."""
    if st.session_state.get("_public_seeded"):
        return
    db = str(_session_db_path())
    init_db(db)
    with transaction(db) as conn:
        cur = conn.execute("SELECT COUNT(*) FROM invoices")
        if int(cur.fetchone()[0]) > 0:
            st.session_state["_public_seeded"] = True
            return
        _insert_seed_row(
            conn, vendor="Prairie Pump Rentals (synthetic)",
            inv_no="DEMO-PPR-001", inv_date="2026-09-15",
            due_date="2026-09-30", po="PO-1042",
            subtotal="5000.00", gst="250.00", total="5250.00",
            currency="CAD",
            description="Equipment rental for wellsite pump.",
            category="Equipment")
        _insert_seed_row(
            conn, vendor="Foothills Lab Services (synthetic)",
            inv_no="DEMO-FL-002", inv_date="2026-09-18",
            due_date="2026-10-02", po="PO-1051",
            subtotal="1800.00", gst="90.00", total="1890.00",
            currency="CAD",
            description="Core analysis - well 14-22.",
            category="Lab Services")
        _insert_seed_row(
            conn, vendor="Northern Transport Inc (synthetic)",
            inv_no="DEMO-NTI-003", inv_date="2026-09-20",
            due_date="2026-10-05", po="",
            subtotal="3200.00", gst="0.00", total="3200.00",
            currency="USD",
            description="Water hauling - battery 03.",
            category="Transport")
    with transaction(db) as conn:
        rows = conn.execute(
            "SELECT id FROM invoices ORDER BY id ASC LIMIT 1"
        ).fetchall()
        if rows:
            first_id = int(rows[0][0])
            approve_invoice(conn, first_id, reviewer="Public Demo Seed")
            mark_paid(conn, first_id, paid_at="2026-09-25T10:00:00",
                      payment_note="seed-paid")
    st.session_state["_public_seeded"] = True


# ---------------------------------------------------------------------------
# Public-mode per-file pipeline.
#
# Reuses the production parse -> extract -> privacy -> validate ->
# save_pending -> save_pdf -> classify chain. classify_expense is
# ALWAYS called with provider=MockProvider() and enable_ai=False so
# no live LLM is ever invoked, even if OPENAI_API_KEY is set in the
# process environment.
# ---------------------------------------------------------------------------
def _run_pipeline_on_sample(pdf_path: Path) -> dict:
    db = str(_session_db_path())
    docs_root = _session_docs_dir()
    # Make sure the schema exists. Idempotent and cheap.
    init_db(db)
    raw_bytes = pdf_path.read_bytes()
    result = {
        "filename": pdf_path.name,
        "status": "failed",
        "vendor": "?",
        "invoice_number": "?",
        "total": "?",
        "currency": "?",
        "category": CATEGORY_OTHER,
        "classification_source": SOURCE_MOCK,
        "validation_errors": 0,
        "validation_warnings": 0,
        "error": None,
        "invoice_id": None,
    }
    try:
        parse_res = parse_pdf(raw_bytes, filename=pdf_path.name)
        raw_text = getattr(parse_res, "raw_text", "") or ""
        if not raw_text or getattr(parse_res, "parsing_status", "ok") != "ok":
            result["error"] = (
                "PDF parse status=" + str(getattr(parse_res, "parsing_status", "?"))
                + " error=" + str(getattr(parse_res, "error", "?"))
            )
            return result
        extraction = extract_invoice(raw_text)
        _ = redact_text(raw_text)
        validation = check_invoice(extraction)
        result["validation_errors"] = int(sum(
            1 for i in validation.issues
            if getattr(i, "severity", "") == "error"))
        result["validation_warnings"] = int(sum(
            1 for i in validation.issues
            if getattr(i, "severity", "") == "warning"))

        with transaction(db) as conn:
            inv_id = int(save_pending(
                conn, extraction,
                source_filename=pdf_path.name,
                expense_category=CATEGORY_OTHER))
            token = _swap_docs_root_for_call(docs_root)
            try:
                stored = save_pdf(raw_bytes, invoice_id=inv_id,
                                  original_filename=pdf_path.name)
            finally:
                _restore_docs_root(token)
            conn.execute(
                "UPDATE invoices SET document_path=?, document_sha256=?, "
                "document_size_bytes=? WHERE id=?",
                (stored.document_path, stored.document_sha256,
                 stored.document_size_bytes, inv_id))
            cls = classify_expense(extraction, provider=MockProvider(),
                                   enable_ai=False)
            conn.execute(
                "UPDATE invoices SET expense_category=? WHERE id=?",
                (cls.category, inv_id))

        result.update({
            "status": "ready",
            "vendor": extraction.vendor_name or "?",
            "invoice_number": extraction.invoice_number or "?",
            "total": str(extraction.total_amount or "?"),
            "currency": extraction.currency or "?",
            "category": cls.category,
            "classification_source": cls.source,
            "invoice_id": inv_id,
        })
    except Exception as exc:
        result["error"] = str(exc)
    return result


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
def page_dashboard() -> None:
    st.header("Dashboard")
    st.caption("Public demo dashboard. All numbers below are derived "
               "from the synthetic invoices in YOUR session only.")
    payload = compute_dashboard(db_path=str(_session_db_path()))
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total invoices", payload.get("total_invoices", 0))
    c2.metric("Pending review", payload.get("pending_count", 0))
    c3.metric("Approved", payload.get("approved_count", 0))
    c4.metric("Paid", payload.get("paid_count", 0))
    rows = payload.get("by_category", []) or []
    if rows:
        st.subheader("Spend by category (mock-classified)")
        st.dataframe(rows, use_container_width=True, hide_index=True)
    else:
        st.info("No invoices in this session yet.")


def page_process_samples() -> None:
    st.header("Process sample invoices")
    st.caption(
        "Upload is disabled. Pick from the committed synthetic PDFs and "
        "click Process to run them through the same parse -> extract -> "
        "validate -> mock-classify pipeline as the desktop build.")
    pdfs = _list_synthetic_pdfs()
    if not pdfs:
        st.error("No synthetic PDFs found at " + str(_DEMO_PDF_DIR))
        return
    labels = [p.name for p in pdfs]
    selected = st.multiselect("Pick one or more synthetic invoices",
                              options=labels, default=labels[:3],
                              key="pd_selected")
    if st.button("Process sample invoices", type="primary",
                 disabled=not selected, key="pd_process_btn"):
        rows = []
        for name in selected:
            pdf_path = next(p for p in pdfs if p.name == name)
            with st.spinner("Processing " + name):
                rows.append(_run_pipeline_on_sample(pdf_path))
        st.session_state["pd_last_results"] = rows
        st.success("Processed " + str(len(rows)) + " synthetic invoice(s).")
    rows = st.session_state.get("pd_last_results")
    if rows:
        st.subheader("Intake queue (this session only)")
        st.dataframe(
            [{
                "Filename": r["filename"], "Vendor": r["vendor"],
                "Invoice #": r["invoice_number"], "Total": r["total"],
                "Currency": r["currency"], "Category": r["category"],
                "Class. source": r["classification_source"],
                "Errors": r["validation_errors"],
                "Warnings": r["validation_warnings"],
                "Status": r["status"],
            } for r in rows],
            use_container_width=True, hide_index=True)


def page_tracker() -> None:
    st.header("Invoice tracker")
    st.caption("Browse, approve, reject, and mark-as-paid in your session. "
               "All changes are confined to this session's temporary "
               "database and evaporate on container restart.")
    db = str(_session_db_path())
    if list_invoices is None or get_invoice is None:
        st.warning("Tracker list/get APIs are not available in this build.")
        return
    invoices = list_invoices(db_path=db)
    if not invoices:
        st.info("No invoices in this session yet. Run 'Process sample "
                "invoices' first.")
        return
    st.dataframe(invoices, use_container_width=True, hide_index=True)
    options = [str(r.get("id")) for r in invoices if r.get("id") is not None]
    if not options:
        return
    inv_id_str = st.selectbox("Invoice id", options=options, key="tk_id")
    if inv_id_str:
        inv = get_invoice(int(inv_id_str), db_path=db)
        if inv is None:
            st.warning("Invoice not found.")
            return
        st.json(inv)
        cols = st.columns(4)
        with cols[0]:
            if st.button("Approve", key="tk_approve"):
                with transaction(db) as conn:
                    approve_invoice(conn, int(inv_id_str),
                                    reviewer="Public Visitor")
                st.success("Approved.")
                st.rerun()
        with cols[1]:
            if st.button("Reject", key="tk_reject"):
                with transaction(db) as conn:
                    reject_invoice(conn, int(inv_id_str),
                                   reviewer="Public Visitor",
                                   reason="Public demo rejection")
                st.success("Rejected.")
                st.rerun()
        with cols[2]:
            if st.button("Mark paid", key="tk_paid"):
                with transaction(db) as conn:
                    mark_paid(conn, int(inv_id_str),
                              paid_at=datetime.utcnow().isoformat(
                                  timespec="seconds"),
                              payment_note="public-demo")
                st.success("Marked paid.")
                st.rerun()


def page_export() -> None:
    st.header("CSV export (synthetic data only)")
    st.caption("Bookkeeping export for the invoices in YOUR session. No "
               "real vendor data is ever included.")
    db = str(_session_db_path())
    try:
        csv_text = build_tracker_csv(db_path=db)
        assert_no_sensitive_columns(csv_text)
    except Exception as exc:
        st.error("Export failed: " + str(exc))
        return
    st.download_button("Download tracker.csv", data=csv_text,
                       file_name="public_demo_tracker.csv", mime="text/csv")


def page_weekly() -> None:
    st.header("Weekly summary (mock narrative)")
    st.caption("The narrative below is generated by a deterministic mock. "
               "No external LLM call is ever made in public mode.")
    payload = build_deterministic_weekly_summary(
        db_path=str(_session_db_path()))
    st.json(payload)


def page_about() -> None:
    st.header("About this public demo")
    st.markdown(
        "**What this demo is**\n\n"
        "* A live, in-browser walkthrough of the OilOps AI pipeline "
        "(parse -> extract -> privacy-filter -> validate -> mock-classify "
        "-> human review -> mark-paid -> dashboard -> export).\n"
        "* Synthetic data only. All vendor names are clearly labelled "
        "`(synthetic)`.\n"
        "* Deterministic mock AI. The `OPENAI_API_KEY` environment "
        "variable is intentionally never consulted, even if present.\n\n"
        "**What this demo is not**\n\n"
        "* Not a production accounting system.\n"
        "* Not connected to any bank, QuickBooks, or email provider.\n"
        "* Not a place to upload real vendor documents. Uploads are "
        "disabled at the UI layer; the backend would refuse them even "
        "if they slipped through.\n"
        "* Not persistent. Session data lives in "
        "`/tmp/oilops_public_sessions/<id>/` and evaporates when the "
        "Streamlit container restarts.\n\n"
        "**Source code:** https://github.com/yolanda418/oilops-ai\n"
    )


PAGES = {
    "Dashboard": page_dashboard,
    "Process sample invoices": page_process_samples,
    "Invoice tracker": page_tracker,
    "Weekly summary": page_weekly,
    "CSV export": page_export,
    "About": page_about,
}


def main() -> None:
    _seed_session_if_empty()
    st.sidebar.title("OilOps AI - Public Demo")
    st.sidebar.caption(
        "Session id: `" + _get_session_id() + "`\n\n"
        "DB: `" + str(_session_db_path()) + "`\n\n"
        "Docs: `" + str(_session_docs_dir()) + "`"
    )
    choice = st.sidebar.radio("Navigate", list(PAGES.keys()),
                              key="public_nav")
    PAGES[choice]()


if __name__ == "__main__":
    main()
