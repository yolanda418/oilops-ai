"""Generate a SYNTHETIC test invoice PDF.

This script writes 'sample_synthetic_invoice.pdf' into the same
directory. The vendor and amounts are entirely fictional.

Run from project root:
    python sample_data/synthetic_invoices/make_synthetic.py

Requires reportlab.
"""

from pathlib import Path


VENDOR = "Rocky Mountain Drilling Services (SYNTHETIC TEST DATA)"
INVOICE_NUMBER = "RMD-2026-001"
PO_NUMBER = "PO-1042"
INVOICE_DATE = "2026-01-15"
DUE_DATE = "2026-02-15"
SUBTOTAL = "$10,000.00"
GST = "$500.00"
TOTAL = "$10,500.00"
DESCRIPTION = "Field service rig rental - January 2026 (SYNTHETIC)"


def main():
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.pdfgen import canvas
    except ImportError:
        print("reportlab is required (pip install reportlab).")
        raise

    out_path = Path(__file__).parent / "sample_synthetic_invoice.pdf"
    c = canvas.Canvas(str(out_path), pagesize=letter)
    width, height = letter

    y_pos = height - 60
    c.setFont("Helvetica-Bold", 16)
    c.drawString(60, y_pos, "INVOICE (SYNTHETIC TEST DATA)")
    y_pos -= 30

    c.setFont("Helvetica", 11)
    lines = [
        VENDOR,
        "Invoice #: " + INVOICE_NUMBER,
        "PO #: " + PO_NUMBER,
        "Invoice Date: " + INVOICE_DATE,
        "Due Date: " + DUE_DATE,
        "Description: " + DESCRIPTION,
        "",
        "Subtotal: " + SUBTOTAL,
        "GST: " + GST,
        "Total: " + TOTAL,
    ]
    for line in lines:
        c.drawString(60, y_pos, line)
        y_pos -= 18

    c.showPage()
    c.save()
    print("Wrote " + str(out_path))


if __name__ == "__main__":
    main()
