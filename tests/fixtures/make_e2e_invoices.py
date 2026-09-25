"""Synthetic invoice fixture generator for STEP 10 E2E tests.

Every value is COMPLETELY FICTIONAL. The fixtures are rendered
dynamically into a pytest tmp_path so the real repo is never polluted.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, List


# All values below are SYNTHETIC test data.

CASE_1_LINES = [
    "INVOICE (SYNTHETIC TEST DATA)",
    "Prairie Pump Rentals",
    "123 Demo Avenue, Calgary AB",
    "",
    "Invoice Number: PPR-1001",
    "PO Number: PO-1001",
    "Invoice Date: 2026-09-15",
    "Due Date: 2026-09-18",
    "Description: Equipment rental - 5 day pump rental (SYNTHETIC)",
    "",
    "Currency: CAD",
    "Subtotal: CAD 5,000.00",
    "GST: CAD 250.00",
    "Total: CAD 5,250.00",
]

CASE_2_LINES = [
    "INVOICE (SYNTHETIC TEST DATA)",
    "Rocky Field Services",
    "99 Demo Street, Edmonton AB",
    "",
    "Invoice Number: RFS-2026-500",
    "PO Number: PO-2026-500",
    "Invoice Date: 2026-09-10",
    "Due Date: 2026-09-20",
    "Description: Field services crew - 10 day contract (SYNTHETIC)",
    "",
    "Currency: CAD",
    "Subtotal: CAD 10,000.00",
    "GST: CAD 500.00",
    "Total: CAD 10,500.00",
]

CASE_3_LINES = [
    "INVOICE (SYNTHETIC TEST DATA)",
    "Calgary Freight Demo",
    "456 Demo Road, Calgary AB",
    "",
    "Invoice Number: CFD-2026-300",
    "Invoice Date: 2026-09-16",
    "Due Date: 2026-09-23",
    "Description: Transportation - 2 load gravel haul (SYNTHETIC)",
    "",
    "Currency: CAD",
    "Subtotal: CAD 2,400.00",
    "GST: CAD 120.00",
    "Total: CAD 2,520.00",
]

CASE_4_LINES = [
    "INVOICE (SYNTHETIC TEST DATA)",
    "Northern Equipment Demo",
    "12 Demo Way, Red Deer AB",
    "",
    "Invoice Number: DUP-001",
    "PO Number: PO-DUP-001",
    "Invoice Date: 2026-09-12",
    "Due Date: 2026-09-19",
    "Description: Equipment rental - compactor (SYNTHETIC)",
    "",
    "Currency: CAD",
    "Subtotal: CAD 1,400.00",
    "GST: CAD 70.00",
    "Total: CAD 1,500.00",
]

CASE_5_LINES = [
    "INVOICE (SYNTHETIC TEST DATA)",
    "Northern Equipment Demo",
    "12 Demo Way, Red Deer AB",
    "",
    "Invoice Number: DUP-001",
    "PO Number: PO-DUP-001",
    "Invoice Date: 2026-09-12",
    "Due Date: 2026-09-19",
    "Description: Equipment rental - compactor (SYNTHETIC duplicate upload)",
    "",
    "Currency: CAD",
    "Subtotal: CAD 1,400.00",
    "GST: CAD 70.00",
    "Total: CAD 1,500.00",
]

CASE_6_LINES = [
    "INVOICE (SYNTHETIC TEST DATA)",
    "US Consulting Demo",
    "999 Demo Boulevard, Houston TX",
    "",
    "Invoice Number: USC-2026-777",
    "PO Number: PO-USC-777",
    "Invoice Date: 2026-09-14",
    "Due Date: 2026-09-28",
    "Description: Professional services - consulting report (SYNTHETIC)",
    "",
    "Currency: USD",
    "Subtotal: USD 3,000.00",
    "Tax: USD 0.00",
    "Total: USD 3,000.00",
]

_CASE_7_BANK_ACCOUNT = "123456789012"
_CASE_7_ROUTING = "021000021"
_CASE_7_SWIFT = "BOFMCAM2"
_CASE_7_IBAN = "GB82WEST12345698765432"
_CASE_7_EMAIL = "billing@example.com"
_CASE_7_PHONE = "(403) 555-0123"

CASE_7_LINES = [
    "INVOICE (SYNTHETIC TEST DATA)",
    "Prairie Field Equipment Ltd. (SYNTHETIC)",
    "789 Demo Crescent, Calgary AB",
    "",
    "Invoice Number: PFE-2026-009",
    "PO Number: PO-8821",
    "Invoice Date: 2026-09-15",
    "Due Date: 2026-10-15",
    "Description: Pump equipment rental (SYNTHETIC)",
    "",
    "Currency: CAD",
    "Subtotal: CAD 5,000.00",
    "GST: CAD 250.00",
    "Total: CAD 5,250.00",
    "",
    "Remit To:",
    "Bank Account: " + _CASE_7_BANK_ACCOUNT,
    "Routing Number: " + _CASE_7_ROUTING,
    "SWIFT: " + _CASE_7_SWIFT,
    "IBAN: " + _CASE_7_IBAN,
    "Email: " + _CASE_7_EMAIL,
    "Phone: " + _CASE_7_PHONE,
]

CASE_8_LINES = [
    "INVOICE (SYNTHETIC TEST DATA)",
    "Invalid Demo Vendor",
    "0 Demo Place, Calgary AB",
    "",
    "PO Number: PO-INVALID-001",
    "Invoice Date: 2026-09-10",
    "Due Date: 2026-09-12",
    "Description: Mismatched amounts (SYNTHETIC - intentionally bad)",
    "",
    "Currency: CAD",
    "Subtotal: CAD 1,000.00",
    "GST: CAD 50.00",
    "Total: CAD 2,000.00",
]


ALL_CASES = [
    ("case1_clean_approved_cad", CASE_1_LINES),
    ("case2_paid_approved_cad", CASE_2_LINES),
    ("case3_missing_po", CASE_3_LINES),
    ("case4_duplicate_a", CASE_4_LINES),
    ("case5_duplicate_b", CASE_5_LINES),
    ("case6_usd_professional", CASE_6_LINES),
    ("case7_sensitive", CASE_7_LINES),
    ("case8_invalid", CASE_8_LINES),
]


def build_all_text():
    return ["\n".join(lines) for _, lines in ALL_CASES]


def write_pdf(path, lines):
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.pdfgen import canvas
    except ImportError as exc:
        raise RuntimeError(
            "reportlab is required to render STEP 10 synthetic PDFs. "
            "Original error: " + str(exc)
        )
    c = canvas.Canvas(str(path), pagesize=letter)
    _, height = letter
    y_pos = height - 60
    c.setFont("Helvetica", 11)
    for line in lines:
        if y_pos < 60:
            c.showPage()
            c.setFont("Helvetica", 11)
            y_pos = height - 60
        c.drawString(60, y_pos, line)
        y_pos -= 16
    c.showPage()
    c.save()
    return path.read_bytes()


def render_all_pdfs(tmp_dir):
    tmp_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for case_name, lines in ALL_CASES:
        p = tmp_dir / (case_name + ".pdf")
        write_pdf(p, lines)
        paths.append(p)
    return paths
